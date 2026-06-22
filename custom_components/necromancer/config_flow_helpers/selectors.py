"""Reactive selectors + selector builders for the Necromancer config flow.

These are pure selector definitions with no flow state. The two `_Live*Selector`
classes bind an attribute/state picker reactively to a sibling entity field via a
per-field `context` mapping the frontend reads on every change.
"""

from __future__ import annotations

from homeassistant.helpers import selector

from ..const import (
    CONF_ATTRIBUTE,
    CONF_ENTITY_ID,
    CONF_ID_ENTITY,
    CONF_STATUS_ATTRIBUTE,
    CONF_STATUS_ENTITY,
)


def _seconds_selector(maximum: int) -> selector.NumberSelector:
    """Build a 0..maximum seconds number box."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=maximum,
            unit_of_measurement="s",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


class _LiveAttributeSelector(selector.AttributeSelector):
    """Attribute picker bound reactively to a sibling entity field.

    A fixed `entity_id` would shadow the frontend's `filter_entity` context
    (`ha-selector-attribute`: `entity_id || filter_entity`), so we drop it and
    emit a per-field `context` mapping instead. `ha-form._generateContext` then
    feeds `filter_entity` from the named sibling field on every change, so the
    attribute list follows the chosen entity. The sibling field name is given so
    the same pattern works for health/verify (`entity_id`) and ports (`id_entity`
    / `status_entity`).
    """

    def __init__(self, entity_field: str) -> None:
        """Bind the attribute picker to the named sibling entity field."""
        # The schema requires an entity_id; it is dropped in serialize() so the
        # context wins. The field name drives the reactive filter.
        super().__init__({"entity_id": "sensor.unknown"})
        self._entity_field = entity_field

    def serialize(self) -> dict:
        """Emit the attribute selector plus its reactive filter_entity context."""
        return {
            "selector": {"attribute": {}},
            "context": {"filter_entity": self._entity_field},
        }


class _LiveStateSelector(selector.StateSelector):
    """State-value picker bound reactively to a sibling entity + attribute.

    Like the automation state trigger's "from/to": it offers the entity's real
    states (or the chosen attribute's values) and still allows free text. Built
    with an empty config; the reactive binding is the per-field `context` mapping
    `ha-selector-state` reads (`filter_entity` / `filter_attribute`), so the value
    list follows the sibling entity and attribute fields live.
    """

    def __init__(self, entity_field: str, attribute_field: str) -> None:
        """Bind the state picker to the named sibling entity + attribute fields."""
        super().__init__({"multiple": True})
        self._entity_field = entity_field
        self._attribute_field = attribute_field

    def serialize(self) -> dict:
        """Emit the state selector plus its reactive entity/attribute filter context."""
        return {
            "selector": {"state": {"multiple": True}},
            "context": {
                "filter_entity": self._entity_field,
                "filter_attribute": self._attribute_field,
            },
        }


_ATTRIBUTE_SELECTOR = _LiveAttributeSelector(CONF_ENTITY_ID)
_HEALTH_VALUE_SELECTOR = _LiveStateSelector(CONF_ENTITY_ID, CONF_ATTRIBUTE)
_ID_ATTRIBUTE = _LiveAttributeSelector(CONF_ID_ENTITY)
_STATUS_ATTRIBUTE = _LiveAttributeSelector(CONF_STATUS_ENTITY)
_STATUS_VALUE_SELECTOR = _LiveStateSelector(CONF_STATUS_ENTITY, CONF_STATUS_ATTRIBUTE)


def _entity_selector(
    exclude: list[str], domain: list[str] | None = None
) -> selector.EntitySelector:
    """Build an entity picker excluding the given entities, optionally domain-scoped."""
    cfg: dict = {"exclude_entities": exclude}
    if domain is not None:
        cfg["domain"] = domain
    return selector.EntitySelector(selector.EntitySelectorConfig(**cfg))
