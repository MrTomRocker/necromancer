"""Config + subentry + options flow for Necromancer.

The integration is a **single service** entry (added once, blank). Every guarded
device is a config **subentry** of type `device`, added via "Add device" and
edited via its "Reconfigure" button.

Recover mode offers seven strategies: power-cycle a `switch`, run one `action`
sequence, or run an off/on pair of `actions` — each with or without a health
check — plus `poe_port` (auto-resolve the device to a PoE port by id). The
health-check variants verify recovery against the device's health entity (the
engine's VERIFY step); the plain ones assume the action worked. Notify mode just
observes.

The health "what to watch" block — entity + attribute (empty = state) + on/off
values — lives in the device step. Recover guards are at most 3 steps (device &
health → strategy → recovery); notify guards are 2.

PoE ports are a single **flat list** managed via the service's **options flow**
(add / edit / delete port). Every `poe_port` guard searches that whole list by
its `expected_id`; there is no per-area grouping.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
import yaml

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import device_registry as dr, entity_registry as er, selector

from .const import (
    CONF_ACTION,
    CONF_ACTUATOR,
    CONF_ATTRIBUTE,
    CONF_BEHAVIOR,
    CONF_BOOT_WINDOW,
    CONF_COOLDOWN,
    CONF_DEBOUNCE,
    CONF_DEVICE_ID,
    CONF_DRIVER,
    CONF_ENTITY_ID,
    CONF_EXPECTED_ID,
    CONF_HEALTH,
    CONF_HEALTH_CHECK,
    CONF_HEALTHY_STATE,
    CONF_ID_ATTRIBUTE,
    CONF_ID_ENTITY,
    CONF_ID_STATIC,
    CONF_IMPORT_MODE,
    CONF_LABEL,
    CONF_LINKED_GUARDS,
    CONF_MAX_ATTEMPTS,
    CONF_MODE,
    CONF_NOTIFY_ACTION,
    CONF_OFF_ACTION,
    CONF_OFF_ON_DELAY,
    CONF_OFF_TIMEOUT,
    CONF_OFF_VALUE,
    CONF_ON_ACTION,
    CONF_ON_TIMEOUT,
    CONF_ON_VALUE,
    CONF_POLICY,
    CONF_PORT_SELECTION,
    CONF_PORTS,
    CONF_PORTS_YAML,
    CONF_SOURCE,
    CONF_SOURCE_TYPE,
    CONF_STATUS_ATTRIBUTE,
    CONF_STATUS_ENTITY,
    CONF_STATUS_OFF,
    CONF_STATUS_ON,
    CONF_STRATEGY,
    CONF_SWITCH_ENTITY,
    CONF_TEMPLATE,
    CONF_TYPE,
    DEFAULT_BOOT_WINDOW,
    DEFAULT_COOLDOWN,
    DEFAULT_DEBOUNCE,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_OFF_ON_DELAY,
    DEFAULT_PORT_OFF_TIMEOUT,
    DEFAULT_PORT_ON_TIMEOUT,
    DOMAIN,
    IMPORT_MODE_MERGE,
    IMPORT_MODE_REPLACE,
    LOGGER,
    MODE_NOTIFY,
    MODE_RECOVER,
    SOURCE_STATE,
    SOURCE_TEMPLATE,
    STRATEGY_ACTION,
    STRATEGY_ACTION_CHECK,
    STRATEGY_ACTIONS,
    STRATEGY_ACTIONS_CHECK,
    STRATEGY_POE,
    STRATEGY_SWITCH,
    STRATEGY_SWITCH_CHECK,
    SUBENTRY_TYPE_DEVICE,
)
from .links import group_of

# Strategies that verify recovery against the health entity (engine VERIFY step).
_CHECK_STRATEGIES = frozenset(
    {STRATEGY_SWITCH_CHECK, STRATEGY_ACTION_CHECK, STRATEGY_ACTIONS_CHECK}
)
# Every recovery strategy offered in the wizard, in display order.
_STRATEGIES = [
    STRATEGY_SWITCH,
    STRATEGY_SWITCH_CHECK,
    STRATEGY_ACTION,
    STRATEGY_ACTION_CHECK,
    STRATEGY_ACTIONS,
    STRATEGY_ACTIONS_CHECK,
    STRATEGY_POE,
]

# Domains that support homeassistant.turn_on/turn_off (incl. template/group
# switch helpers, which are plain `switch` entities, and input_boolean toggles).
_SWITCH_DOMAINS = [
    "switch",
    "input_boolean",
    "light",
    "fan",
    "siren",
    "humidifier",
    "remote",
    "media_player",
    "group",
]


def _seconds_selector(maximum: int) -> selector.NumberSelector:
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
        # The schema requires an entity_id; it is dropped in serialize() so the
        # context wins. The field name drives the reactive filter.
        super().__init__({"entity_id": "sensor.unknown"})
        self._entity_field = entity_field

    def serialize(self) -> dict:
        return {
            "selector": {"attribute": {}},
            "context": {"filter_entity": self._entity_field},
        }


_ATTRIBUTE_SELECTOR = _LiveAttributeSelector(CONF_ENTITY_ID)


class _LiveStateSelector(selector.StateSelector):
    """State-value picker bound reactively to a sibling entity + attribute.

    Like the automation state trigger's "from/to": it offers the entity's real
    states (or the chosen attribute's values) and still allows free text. Built
    with an empty config; the reactive binding is the per-field `context` mapping
    `ha-selector-state` reads (`filter_entity` / `filter_attribute`), so the value
    list follows the sibling entity and attribute fields live.
    """

    def __init__(self, entity_field: str, attribute_field: str) -> None:
        super().__init__({"multiple": True})
        self._entity_field = entity_field
        self._attribute_field = attribute_field

    def serialize(self) -> dict:
        return {
            "selector": {"state": {"multiple": True}},
            "context": {
                "filter_entity": self._entity_field,
                "filter_attribute": self._attribute_field,
            },
        }


def _as_list(value: object) -> list:
    """A stored value may be a bare string (legacy) or a list; return a list."""
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


_HEALTH_VALUE_SELECTOR = _LiveStateSelector(CONF_ENTITY_ID, CONF_ATTRIBUTE)


def _watch_fields(d: dict) -> dict:
    """Attribute (empty = state) + on/off value pickers for an entity.

    Both the attribute dropdown and the on/off value pickers follow the sibling
    `entity_id` field live via a form `context` mapping (see `_LiveAttributeSelector`
    / `_LiveStateSelector`); the value pickers also follow the chosen attribute.
    """
    on = _as_list(d.get(CONF_ON_VALUE)) or ["on"]
    off = _as_list(d.get(CONF_OFF_VALUE)) or ["off"]
    return {
        vol.Optional(
            CONF_ATTRIBUTE, description={"suggested_value": d.get(CONF_ATTRIBUTE)}
        ): _ATTRIBUTE_SELECTOR,
        vol.Required(CONF_ON_VALUE, default=on): _HEALTH_VALUE_SELECTOR,
        vol.Required(CONF_OFF_VALUE, default=off): _HEALTH_VALUE_SELECTOR,
    }


# Form sections group fields under a visible heading + description (some selectors
# like Device/Action don't render their own label). A section nests its fields'
# values, so submitted input is flattened back up before use. Each key is also a
# translation key: sections.<key>.name / .description.
SECTION_STATE = "state_check"
SECTION_TEMPLATE = "template_check"
SECTION_DEVICE = "assigned_device"
SECTION_LINK = "linked_guards"
SECTION_BEHAVIOR = "behavior"
SECTION_NOTIFY = "notification"
SECTION_POWER = "power"
SECTION_IDENTITY = "identity"
SECTION_STATUS = "status"
SECTION_TIMING = "timing"


def _section(key: str, fields: dict, *, collapsed: bool = False) -> dict:
    """Wrap a group of fields in a collapsible, titled section."""
    return {vol.Required(key): section(vol.Schema(fields), {"collapsed": collapsed})}


def _flatten_sections(user_input: dict) -> dict:
    """Lift section sub-dicts back to the top level (sections nest their fields)."""
    out: dict = {}
    for key, value in user_input.items():
        if isinstance(value, dict):
            out.update(value)
        else:
            out[key] = value
    return out


def _own_entities(hass: HomeAssistant) -> list[str]:
    """Necromancer's own entities (excluded from the entity pickers)."""
    ent_reg = er.async_get(hass)
    return [e.entity_id for e in ent_reg.entities.values() if e.platform == DOMAIN]


def _entity_selector(
    exclude: list[str], domain: list[str] | None = None
) -> selector.EntitySelector:
    cfg: dict = {"exclude_entities": exclude}
    if domain is not None:
        cfg["domain"] = domain
    return selector.EntitySelector(selector.EntitySelectorConfig(**cfg))


def _source_schema(default: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_SOURCE_TYPE, default=default): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[SOURCE_STATE, SOURCE_TEMPLATE],
                    translation_key="source_type",
                    mode=selector.SelectSelectorMode.LIST,
                )
            )
        }
    )


def _source_type_of(data: dict) -> str:
    is_template = data.get(CONF_HEALTH, {}).get(CONF_TYPE) == "template"
    return SOURCE_TEMPLATE if is_template else SOURCE_STATE


def _health_section(d: dict, *, source_type: str, exclude: list[str]) -> dict:
    """The state-detection block, depending on the chosen source type."""
    if source_type == SOURCE_TEMPLATE:
        return _section(
            SECTION_TEMPLATE,
            {
                vol.Required(
                    CONF_TEMPLATE, default=d.get(CONF_TEMPLATE, "")
                ): selector.TemplateSelector()
            },
        )
    return _section(
        SECTION_STATE,
        {
            vol.Required(
                CONF_ENTITY_ID, default=d.get(CONF_ENTITY_ID, vol.UNDEFINED)
            ): _entity_selector(list(exclude)),
            **_watch_fields(d),
        },
    )


def _device_schema(
    d: dict | None = None, *, source_type: str = SOURCE_STATE, exclude: list[str] = ()
) -> vol.Schema:
    d = d or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=d.get(CONF_NAME, "")): str,
            **_health_section(d, source_type=source_type, exclude=list(exclude)),
            vol.Required(
                CONF_MODE, default=d.get(CONF_MODE, MODE_RECOVER)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[MODE_RECOVER, MODE_NOTIFY],
                    translation_key="mode",
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            **_section(
                SECTION_DEVICE,
                {
                    vol.Optional(
                        CONF_DEVICE_ID,
                        description={"suggested_value": d.get(CONF_DEVICE_ID)},
                    ): selector.DeviceSelector(),
                },
            ),
        }
    )


def _strategy_schema(default: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_STRATEGY, default=default): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(_STRATEGIES),
                    translation_key="strategy",
                    mode=selector.SelectSelectorMode.LIST,
                )
            )
        }
    )


def _notification_section(d: dict) -> dict:
    """The optional alert action, in its own section.

    The section heading + description carry the variable hint the ActionSelector
    can't render itself; the user decides whether/how to notify.
    """
    return _section(
        SECTION_NOTIFY,
        {
            vol.Optional(
                CONF_NOTIFY_ACTION,
                description={"suggested_value": d.get(CONF_NOTIFY_ACTION)},
            ): selector.ActionSelector()
        },
    )


def _link_section(options: list[dict], default: list[str]) -> dict:
    """Collapsed multi-select of group partners (other recover guards).

    Returns an empty dict when there are no other recover guards to link to, so
    the section is simply omitted (an empty SelectSelector would be pointless).
    """
    if not options:
        return {}
    return _section(
        SECTION_LINK,
        {
            vol.Optional(CONF_LINKED_GUARDS, default=default): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            )
        },
        collapsed=True,
    )


def _debounce_field(d: dict) -> dict:
    return {
        vol.Required(
            CONF_DEBOUNCE, default=d.get(CONF_DEBOUNCE, DEFAULT_DEBOUNCE)
        ): _seconds_selector(3600)
    }


def _behavior_section(d: dict, *, check: bool) -> dict:
    """Timing/retry behaviour, in a section collapsed by default (good defaults).

    With a health-check, recovery is verified against the health entity, so the
    boot window (time to come back) and retry count apply; without it the action
    is fire-and-forget and both are omitted.
    """
    fields: dict = {**_debounce_field(d)}
    if check:
        fields[
            vol.Required(
                CONF_BOOT_WINDOW, default=d.get(CONF_BOOT_WINDOW, DEFAULT_BOOT_WINDOW)
            )
        ] = _seconds_selector(3600)
    fields[
        vol.Required(CONF_COOLDOWN, default=d.get(CONF_COOLDOWN, DEFAULT_COOLDOWN))
    ] = _seconds_selector(86400)
    if check:
        fields[
            vol.Required(
                CONF_MAX_ATTEMPTS,
                default=d.get(CONF_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS),
            )
        ] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=10, mode=selector.NumberSelectorMode.BOX
            )
        )
    # Auto-recovery is not a setup field: it's the per-guard runtime switch entity
    # (persisted), so guards start with it on (DEFAULT_AUTO_RESTART).
    return _section(SECTION_BEHAVIOR, fields)


def _switch_fields(d: dict, exclude: list[str]) -> dict:
    return {
        vol.Required(
            CONF_SWITCH_ENTITY, default=d.get(CONF_SWITCH_ENTITY, vol.UNDEFINED)
        ): _entity_selector(exclude, _SWITCH_DOMAINS),
        vol.Required(
            CONF_OFF_ON_DELAY, default=d.get(CONF_OFF_ON_DELAY, DEFAULT_OFF_ON_DELAY)
        ): _seconds_selector(600),
    }


def _switch_schema(
    d: dict | None = None, *, check: bool, exclude: list[str] = ()
) -> vol.Schema:
    d = d or {}
    return vol.Schema(
        {
            **_switch_fields(d, list(exclude)),
            **_behavior_section(d, check=check),
            **_notification_section(d),
        }
    )


def _action_schema(d: dict | None = None, *, check: bool) -> vol.Schema:
    """One recovery action sequence + behaviour."""
    d = d or {}
    return vol.Schema(
        {
            vol.Optional(
                CONF_ACTION, description={"suggested_value": d.get(CONF_ACTION)}
            ): selector.ActionSelector(),
            **_behavior_section(d, check=check),
            **_notification_section(d),
        }
    )


def _actions_schema(d: dict | None = None, *, check: bool) -> vol.Schema:
    """An "off" and an "on" action sequence + delay + behaviour."""
    d = d or {}
    return vol.Schema(
        {
            vol.Optional(
                CONF_OFF_ACTION, description={"suggested_value": d.get(CONF_OFF_ACTION)}
            ): selector.ActionSelector(),
            vol.Optional(
                CONF_ON_ACTION, description={"suggested_value": d.get(CONF_ON_ACTION)}
            ): selector.ActionSelector(),
            vol.Required(
                CONF_OFF_ON_DELAY,
                default=d.get(CONF_OFF_ON_DELAY, DEFAULT_OFF_ON_DELAY),
            ): _seconds_selector(600),
            **_behavior_section(d, check=check),
            **_notification_section(d),
        }
    )


def _notify_schema(d: dict | None = None) -> vol.Schema:
    d = d or {}
    return vol.Schema({**_debounce_field(d), **_notification_section(d)})


def _watch_config(block: dict) -> dict:
    """Source (attribute or state) + on/off values from a submitted step."""
    return {
        CONF_SOURCE: block.get(CONF_ATTRIBUTE) or "state",
        CONF_ON_VALUE: block[CONF_ON_VALUE],
        CONF_OFF_VALUE: block[CONF_OFF_VALUE],
    }


def _build_driver(step2: dict, strategy: str) -> dict:
    if strategy == STRATEGY_POE:
        return {CONF_TYPE: "poe_port", CONF_EXPECTED_ID: step2[CONF_EXPECTED_ID]}
    if strategy in (STRATEGY_ACTION, STRATEGY_ACTION_CHECK):
        return {CONF_TYPE: "action_call", CONF_ACTION: step2.get(CONF_ACTION)}
    if strategy in (STRATEGY_ACTIONS, STRATEGY_ACTIONS_CHECK):
        return {
            CONF_TYPE: "action_cycle",
            CONF_OFF_ACTION: step2.get(CONF_OFF_ACTION),
            CONF_ON_ACTION: step2.get(CONF_ON_ACTION),
            CONF_OFF_ON_DELAY: int(step2[CONF_OFF_ON_DELAY]),
        }
    return {
        CONF_TYPE: "switch_cycle",
        CONF_SWITCH_ENTITY: step2[CONF_SWITCH_ENTITY],
        CONF_OFF_ON_DELAY: int(step2[CONF_OFF_ON_DELAY]),
    }


def _build_data(step1: dict, step2: dict, strategy: str) -> dict:
    step1 = _flatten_sections(step1)
    step2 = _flatten_sections(step2)
    notify_only = step1.get(CONF_MODE) == MODE_NOTIFY
    check = strategy in _CHECK_STRATEGIES or strategy == STRATEGY_POE
    behavior = {
        CONF_DEBOUNCE: int(step2[CONF_DEBOUNCE]),
        CONF_NOTIFY_ACTION: step2.get(CONF_NOTIFY_ACTION),
    }
    if step1.get(CONF_SOURCE_TYPE) == SOURCE_TEMPLATE:
        health = {CONF_TYPE: "template", CONF_TEMPLATE: step1[CONF_TEMPLATE]}
    else:
        health = {
            CONF_TYPE: "entity_state",
            CONF_ENTITY_ID: step1[CONF_ENTITY_ID],
            **_watch_config(step1),
        }
    data = {
        CONF_NAME: step1[CONF_NAME],
        CONF_HEALTH: health,
        CONF_POLICY: {CONF_TYPE: MODE_NOTIFY if notify_only else "standard"},
        CONF_BEHAVIOR: behavior,
    }
    if notify_only:
        data[CONF_DRIVER] = {CONF_TYPE: "noop"}
    else:
        behavior[CONF_COOLDOWN] = int(step2[CONF_COOLDOWN])
        behavior[CONF_HEALTH_CHECK] = check
        if check:
            behavior[CONF_BOOT_WINDOW] = int(step2[CONF_BOOT_WINDOW])
            behavior[CONF_MAX_ATTEMPTS] = int(step2[CONF_MAX_ATTEMPTS])
        data[CONF_DRIVER] = _build_driver(step2, strategy)
    if step1.get(CONF_DEVICE_ID):
        data[CONF_DEVICE_ID] = step1[CONF_DEVICE_ID]
    if not notify_only and step2.get(CONF_LINKED_GUARDS):
        data[CONF_LINKED_GUARDS] = sorted(step2[CONF_LINKED_GUARDS])
    return data


def _current_strategy(data: dict) -> str:
    driver = data.get(CONF_DRIVER, {})
    dtype = driver.get(CONF_TYPE)
    check = bool(data.get(CONF_BEHAVIOR, {}).get(CONF_HEALTH_CHECK))
    if dtype == "poe_port":
        return STRATEGY_POE
    if dtype == "action_cycle":
        return STRATEGY_ACTIONS_CHECK if check else STRATEGY_ACTIONS
    if dtype == "action_call":
        return STRATEGY_ACTION_CHECK if check else STRATEGY_ACTION
    return STRATEGY_SWITCH_CHECK if check else STRATEGY_SWITCH


def _watch_defaults(block: dict) -> dict:
    """Flatten a stored health/verify block into _watch_fields defaults."""
    source = block.get(CONF_SOURCE, "state")
    return {
        CONF_ATTRIBUTE: None if source == "state" else source,
        CONF_ON_VALUE: block.get(CONF_ON_VALUE) or block.get(CONF_HEALTHY_STATE, "on"),
        CONF_OFF_VALUE: block.get(CONF_OFF_VALUE, "off"),
    }


def _health_defaults(data: dict) -> dict:
    health = data.get(CONF_HEALTH, {})
    is_notify = data.get(CONF_POLICY, {}).get(CONF_TYPE) == MODE_NOTIFY
    return {
        CONF_NAME: data.get(CONF_NAME, ""),
        CONF_ENTITY_ID: health.get(CONF_ENTITY_ID),
        CONF_TEMPLATE: health.get(CONF_TEMPLATE, ""),
        **_watch_defaults(health),
        CONF_MODE: MODE_NOTIFY if is_notify else MODE_RECOVER,
        CONF_DEVICE_ID: data.get(CONF_DEVICE_ID),
    }


def _behavior_defaults(data: dict) -> dict:
    b = data.get(CONF_BEHAVIOR, {})
    return {
        CONF_DEBOUNCE: b.get(CONF_DEBOUNCE, DEFAULT_DEBOUNCE),
        CONF_BOOT_WINDOW: b.get(CONF_BOOT_WINDOW, DEFAULT_BOOT_WINDOW),
        CONF_COOLDOWN: b.get(CONF_COOLDOWN, DEFAULT_COOLDOWN),
        CONF_MAX_ATTEMPTS: b.get(CONF_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS),
        CONF_NOTIFY_ACTION: b.get(CONF_NOTIFY_ACTION),
    }


def _switch_defaults(data: dict) -> dict:
    drv = data.get(CONF_DRIVER, {})
    return {
        **_behavior_defaults(data),
        CONF_SWITCH_ENTITY: drv.get(CONF_SWITCH_ENTITY, vol.UNDEFINED),
        CONF_OFF_ON_DELAY: drv.get(CONF_OFF_ON_DELAY, DEFAULT_OFF_ON_DELAY),
    }


def _action_defaults(data: dict) -> dict:
    return {
        **_behavior_defaults(data),
        CONF_ACTION: data.get(CONF_DRIVER, {}).get(CONF_ACTION),
    }


def _actions_defaults(data: dict) -> dict:
    drv = data.get(CONF_DRIVER, {})
    return {
        **_behavior_defaults(data),
        CONF_OFF_ACTION: drv.get(CONF_OFF_ACTION),
        CONF_ON_ACTION: drv.get(CONF_ON_ACTION),
        CONF_OFF_ON_DELAY: drv.get(CONF_OFF_ON_DELAY, DEFAULT_OFF_ON_DELAY),
    }


def _poe_defaults(data: dict) -> dict:
    drv = data.get(CONF_DRIVER, {})
    return {
        **_behavior_defaults(data),
        CONF_EXPECTED_ID: drv.get(CONF_EXPECTED_ID, ""),
    }


def _poe_schema(d: dict | None = None) -> vol.Schema:
    d = d or {}
    return vol.Schema(
        {
            vol.Required(CONF_EXPECTED_ID, default=d.get(CONF_EXPECTED_ID, "")): str,
            **_behavior_section(d, check=True),
            **_notification_section(d),
        }
    )


# ---------- search-area ports ----------
_ID_ATTRIBUTE = _LiveAttributeSelector(CONF_ID_ENTITY)
_STATUS_ATTRIBUTE = _LiveAttributeSelector(CONF_STATUS_ENTITY)
_STATUS_VALUE_SELECTOR = _LiveStateSelector(CONF_STATUS_ENTITY, CONF_STATUS_ATTRIBUTE)


def _port_schema(d: dict | None = None, *, exclude: list[str] = ()) -> vol.Schema:
    """One port, grouped into sections: switch · identity · status · timing.

    The reactive id/status pickers stay in the same section as the entity field
    they follow (ha-form resolves a field's `context` from its own section).
    """
    d = d or {}
    exclude = list(exclude)
    return vol.Schema(
        {
            vol.Required(CONF_LABEL, default=d.get(CONF_LABEL, "")): str,
            **_section(
                SECTION_POWER,
                {
                    vol.Required(
                        CONF_ACTUATOR, default=d.get(CONF_ACTUATOR, vol.UNDEFINED)
                    ): _entity_selector(exclude, ["switch", "input_boolean"]),
                },
            ),
            **_section(
                SECTION_IDENTITY,
                {
                    vol.Optional(
                        CONF_ID_ENTITY,
                        description={"suggested_value": d.get(CONF_ID_ENTITY)},
                    ): _entity_selector(exclude),
                    vol.Optional(
                        CONF_ID_ATTRIBUTE,
                        description={"suggested_value": d.get(CONF_ID_ATTRIBUTE)},
                    ): _ID_ATTRIBUTE,
                    vol.Optional(
                        CONF_ID_STATIC,
                        description={"suggested_value": d.get(CONF_ID_STATIC)},
                    ): selector.TextSelector(),
                },
            ),
            **_section(
                SECTION_STATUS,
                {
                    vol.Required(
                        CONF_STATUS_ENTITY,
                        default=d.get(CONF_STATUS_ENTITY, vol.UNDEFINED),
                    ): _entity_selector(exclude),
                    vol.Optional(
                        CONF_STATUS_ATTRIBUTE,
                        description={"suggested_value": d.get(CONF_STATUS_ATTRIBUTE)},
                    ): _STATUS_ATTRIBUTE,
                    vol.Required(
                        CONF_STATUS_ON,
                        default=_as_list(d.get(CONF_STATUS_ON)) or ["on"],
                    ): _STATUS_VALUE_SELECTOR,
                    vol.Required(
                        CONF_STATUS_OFF,
                        default=_as_list(d.get(CONF_STATUS_OFF)) or ["off"],
                    ): _STATUS_VALUE_SELECTOR,
                },
            ),
            **_section(
                SECTION_TIMING,
                {
                    vol.Required(
                        CONF_OFF_ON_DELAY,
                        default=d.get(CONF_OFF_ON_DELAY, DEFAULT_OFF_ON_DELAY),
                    ): _seconds_selector(600),
                    vol.Required(
                        CONF_OFF_TIMEOUT,
                        default=d.get(CONF_OFF_TIMEOUT, DEFAULT_PORT_OFF_TIMEOUT),
                    ): _seconds_selector(600),
                    vol.Required(
                        CONF_ON_TIMEOUT,
                        default=d.get(CONF_ON_TIMEOUT, DEFAULT_PORT_ON_TIMEOUT),
                    ): _seconds_selector(3600),
                },
            ),
        }
    )


class NecromancerConfigFlow(ConfigFlow, domain=DOMAIN):
    """A single blank service entry.

    Guarded devices are `device` subentries; PoE ports are a flat list in the
    entry's options (the options flow).
    """

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        return self.async_create_entry(title="Necromancer", data={})

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TYPE_DEVICE: DeviceSubentryFlow}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return NecromancerOptionsFlow()


class DeviceSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure one guarded device. Add and reconfigure share steps."""

    def __init__(self) -> None:
        self._reconfig = False
        self._source_type = SOURCE_STATE
        self._step1: dict = {}
        self._strategy = STRATEGY_SWITCH

    def _is_own_device(self, device_id: str) -> bool:
        """True if the device belongs to Necromancer (block self/cross links)."""
        device = dr.async_get(self.hass).async_get(device_id)
        return device is not None and any(
            domain == DOMAIN for domain, _ in device.identifiers
        )

    def _reconfig_data(self) -> dict:
        return self._get_reconfigure_subentry().data

    def _name_taken(self, name: str) -> bool:
        """True if another guard already uses this name (case/space-insensitive)."""
        wanted = (name or "").strip().casefold()
        if not wanted:
            return False
        own = self._get_reconfigure_subentry().subentry_id if self._reconfig else None
        return any(
            sid != own
            and (se.data.get(CONF_NAME) or se.title or "").strip().casefold() == wanted
            for sid, se in self._get_entry().subentries.items()
            if se.subentry_type == SUBENTRY_TYPE_DEVICE
        )

    # ---------- guard linking ----------
    def _recover_guards(self) -> dict[str, dict]:
        """All recover-mode device subentries by id (notify guards can't link)."""
        return {
            sid: dict(se.data)
            for sid, se in self._get_entry().subentries.items()
            if se.subentry_type == SUBENTRY_TYPE_DEVICE
            and se.data.get(CONF_POLICY, {}).get(CONF_TYPE) != MODE_NOTIFY
        }

    def _own_subentry_id(self) -> str | None:
        return self._get_reconfigure_subentry().subentry_id if self._reconfig else None

    def _link_options(self) -> list[dict]:
        """Pickable partners: every other recover guard."""
        own = self._own_subentry_id()
        return [
            {"value": sid, "label": data.get(CONF_NAME) or sid}
            for sid, data in self._recover_guards().items()
            if sid != own
        ]

    def _linked_default(self) -> list[str]:
        """Current group of the edited guard (clique-closed), for the form."""
        own = self._own_subentry_id()
        if own is None:
            return []
        guards = self._recover_guards()
        links = {
            sid: set(data.get(CONF_LINKED_GUARDS, []) or [])
            for sid, data in guards.items()
        }
        return sorted(group_of(links, set(guards), own))

    def _with_link(self, schema: vol.Schema) -> vol.Schema:
        """Append the collapsed link section to a recover-strategy schema."""
        section_dict = _link_section(self._link_options(), self._linked_default())
        return schema.extend(section_dict) if section_dict else schema

    # ---------- source type (entity state vs template) ----------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._source(user_input, reconfig=False)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._source(user_input, reconfig=True)

    async def _source(
        self, user_input: dict[str, Any] | None, *, reconfig: bool
    ) -> SubentryFlowResult:
        self._reconfig = reconfig
        if user_input is not None:
            self._source_type = user_input[CONF_SOURCE_TYPE]
            return await self.async_step_device()
        default = _source_type_of(self._reconfig_data()) if reconfig else SOURCE_STATE
        return self.async_show_form(
            step_id="reconfigure" if reconfig else "user",
            data_schema=_source_schema(default),
        )

    # ---------- device & health ----------
    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = _flatten_sections(user_input)
            did = user_input.get(CONF_DEVICE_ID)
            if did and self._is_own_device(did):
                errors[CONF_DEVICE_ID] = "no_self_link"
            elif self._name_taken(user_input.get(CONF_NAME, "")):
                # Distinct names keep entity_ids (sensor.<name>_status) unambiguous.
                errors[CONF_NAME] = "duplicate_name"
            else:
                user_input[CONF_SOURCE_TYPE] = self._source_type
                self._step1 = user_input
                if user_input.get(CONF_MODE) == MODE_NOTIFY:
                    return await self.async_step_notify()
                return await self.async_step_strategy()
            defaults = user_input
        elif self._reconfig:
            defaults = _health_defaults(self._reconfig_data())
        else:
            defaults = None
        return self.async_show_form(
            step_id="device",
            data_schema=_device_schema(
                defaults,
                source_type=self._source_type,
                exclude=_own_entities(self.hass),
            ),
            errors=errors,
        )

    # ---------- strategy select (recover) ----------
    async def async_step_strategy(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            self._strategy = user_input[CONF_STRATEGY]
            return await {
                STRATEGY_SWITCH: self.async_step_switch,
                STRATEGY_SWITCH_CHECK: self.async_step_switch,
                STRATEGY_ACTION: self.async_step_action,
                STRATEGY_ACTION_CHECK: self.async_step_action,
                STRATEGY_ACTIONS: self.async_step_actions,
                STRATEGY_ACTIONS_CHECK: self.async_step_actions,
                STRATEGY_POE: self.async_step_poe_port,
            }[self._strategy]()
        default = (
            _current_strategy(self._reconfig_data())
            if self._reconfig
            else STRATEGY_SWITCH
        )
        return self.async_show_form(
            step_id="strategy", data_schema=_strategy_schema(default)
        )

    @property
    def _check(self) -> bool:
        return self._strategy in _CHECK_STRATEGIES

    # ---------- recovery strategy forms (one step per action shape) ----------
    async def async_step_switch(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            return await self._finish(
                _build_data(self._step1, user_input, self._strategy)
            )
        d = _switch_defaults(self._reconfig_data()) if self._reconfig else None
        return self.async_show_form(
            step_id="switch",
            data_schema=self._with_link(
                _switch_schema(d, check=self._check, exclude=_own_entities(self.hass))
            ),
        )

    async def async_step_action(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            flat = _flatten_sections(user_input)
            if not flat.get(CONF_ACTION):
                # An action guard with no action can only ever escalate — reject it
                # here instead of letting it fail at runtime.
                errors[CONF_ACTION] = "action_required"
            else:
                return await self._finish(
                    _build_data(self._step1, user_input, self._strategy)
                )
            d = flat
        else:
            d = _action_defaults(self._reconfig_data()) if self._reconfig else None
        return self.async_show_form(
            step_id="action",
            data_schema=self._with_link(_action_schema(d, check=self._check)),
            errors=errors,
        )

    async def async_step_actions(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            flat = _flatten_sections(user_input)
            if not flat.get(CONF_OFF_ACTION):
                errors[CONF_OFF_ACTION] = "action_required"
            if not flat.get(CONF_ON_ACTION):
                errors[CONF_ON_ACTION] = "action_required"
            if not errors:
                return await self._finish(
                    _build_data(self._step1, user_input, self._strategy)
                )
            d = flat
        else:
            d = _actions_defaults(self._reconfig_data()) if self._reconfig else None
        return self.async_show_form(
            step_id="actions",
            data_schema=self._with_link(_actions_schema(d, check=self._check)),
            errors=errors,
        )

    # ---------- poe_port (auto-resolve against the flat port list) ----------
    async def async_step_poe_port(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            return await self._finish(
                _build_data(self._step1, user_input, STRATEGY_POE)
            )
        d = _poe_defaults(self._reconfig_data()) if self._reconfig else None
        return self.async_show_form(
            step_id="poe_port", data_schema=self._with_link(_poe_schema(d))
        )

    # ---------- notify-only ----------
    async def async_step_notify(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            return await self._finish(
                _build_data(self._step1, user_input, STRATEGY_SWITCH)
            )
        d = _behavior_defaults(self._reconfig_data()) if self._reconfig else None
        return self.async_show_form(step_id="notify", data_schema=_notify_schema(d))

    # ---------- create / update ----------
    def _apply_link_removals(self, subentry, data: dict) -> None:
        """Clear our id from any partner we just unlinked (keep links symmetric).

        Additions stay one-sided — the runtime/form closure re-groups them; only a
        removal must break the edge on both ends, else the closure pulls it back.
        """
        old = self._linked_default()  # the group shown in the form (clique-closed)
        new = set(data.get(CONF_LINKED_GUARDS, []) or [])
        entry = self._get_entry()
        for partner_id in set(old) - new:
            partner = entry.subentries.get(partner_id)
            if partner is None:
                continue
            kept = [
                x
                for x in (partner.data.get(CONF_LINKED_GUARDS, []) or [])
                if x != subentry.subentry_id
            ]
            if kept != (partner.data.get(CONF_LINKED_GUARDS, []) or []):
                self.hass.config_entries.async_update_subentry(
                    entry, partner, data={**partner.data, CONF_LINKED_GUARDS: kept}
                )

    async def _finish(self, data: dict) -> SubentryFlowResult:
        if not self._reconfig:
            LOGGER.debug("Creating guard subentry for %s", data[CONF_NAME])
            return self.async_create_entry(title=data[CONF_NAME], data=data)
        subentry = self._get_reconfigure_subentry()
        # On unlink (had a device, now none): flag it so setup resets the device's
        # display name to the guard name after the reload. A plain rename must not.
        if subentry.data.get(CONF_DEVICE_ID) and not data.get(CONF_DEVICE_ID):
            self.hass.data.setdefault(DOMAIN, {}).setdefault("name_reset", set()).add(
                subentry.subentry_id
            )
        self._apply_link_removals(subentry, data)
        LOGGER.debug("Reconfiguring guard subentry for %s", data[CONF_NAME])
        return self.async_update_and_abort(
            self._get_entry(), subentry, title=data[CONF_NAME], data=data
        )


def _port_select_schema(ports: list[dict]) -> vol.Schema:
    """Radio-button pick of an existing port (by index)."""
    options = [
        {"value": str(i), "label": p.get(CONF_LABEL) or f"Port {i + 1}"}
        for i, p in enumerate(ports)
    ]
    return vol.Schema(
        {
            vol.Required("port"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options, mode=selector.SelectSelectorMode.LIST
                )
            )
        }
    )


# --- port import / export (YAML bulk-edit escape hatch) ---

# Field order for clean, round-trippable YAML export.
_PORT_EXPORT_KEYS = (
    CONF_LABEL,
    CONF_ACTUATOR,
    CONF_ID_ENTITY,
    CONF_ID_ATTRIBUTE,
    CONF_ID_STATIC,
    CONF_STATUS_ENTITY,
    CONF_STATUS_ATTRIBUTE,
    CONF_STATUS_ON,
    CONF_STATUS_OFF,
    CONF_OFF_ON_DELAY,
    CONF_OFF_TIMEOUT,
    CONF_ON_TIMEOUT,
)


def _yaml_value(value: object) -> object:
    """YAML 1.1 reads on/off/yes/no as booleans; map those back to on/off."""
    if isinstance(value, bool):
        return "on" if value else "off"
    return value


def _str_values(raw: object) -> list[str]:
    """Normalise a status value (scalar or list) to a list of strings."""
    return [str(_yaml_value(v)).strip() for v in _as_list(raw)]


def _normalize_imported_port(raw: object) -> dict:
    """Validate one imported port and return it in the stored shape.

    Raises ValueError with a user-facing reason on a missing/invalid field.
    """
    if not isinstance(raw, dict):
        raise ValueError("each entry must be a port mapping")
    label = str(raw.get(CONF_LABEL, "")).strip()
    if not label:
        raise ValueError("a port is missing 'label'")
    actuator = str(raw.get(CONF_ACTUATOR, "")).strip()
    if not actuator:
        raise ValueError(f"port '{label}' is missing 'actuator'")
    status_entity = str(raw.get(CONF_STATUS_ENTITY, "")).strip()
    if not status_entity:
        raise ValueError(f"port '{label}' is missing 'status_entity'")
    try:
        timing = {
            CONF_OFF_ON_DELAY: float(raw.get(CONF_OFF_ON_DELAY, DEFAULT_OFF_ON_DELAY)),
            CONF_OFF_TIMEOUT: float(
                raw.get(CONF_OFF_TIMEOUT, DEFAULT_PORT_OFF_TIMEOUT)
            ),
            CONF_ON_TIMEOUT: float(raw.get(CONF_ON_TIMEOUT, DEFAULT_PORT_ON_TIMEOUT)),
        }
    except (TypeError, ValueError) as err:
        raise ValueError(f"port '{label}' has a non-numeric timing value") from err
    if any(value < 0 for value in timing.values()):
        raise ValueError(f"port '{label}' has a negative timing value")
    port: dict = {
        CONF_LABEL: label,
        CONF_ACTUATOR: actuator,
        CONF_STATUS_ENTITY: status_entity,
        CONF_STATUS_ON: _str_values(raw.get(CONF_STATUS_ON)) or ["on"],
        CONF_STATUS_OFF: _str_values(raw.get(CONF_STATUS_OFF)) or ["off"],
        **timing,
    }
    for key in (
        CONF_ID_ENTITY,
        CONF_ID_ATTRIBUTE,
        CONF_ID_STATIC,
        CONF_STATUS_ATTRIBUTE,
    ):
        value = raw.get(key)
        if value not in (None, ""):
            port[key] = str(_yaml_value(value)).strip()
    return port


def _parse_ports_yaml(text: str) -> list[dict]:
    """Parse and validate pasted YAML into a clean port list (raises ValueError)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ValueError(f"not valid YAML ({err.__class__.__name__})") from err
    if data is None:
        raise ValueError("no ports given")
    if not isinstance(data, list):
        raise ValueError("expected a list of ports")
    return [_normalize_imported_port(port) for port in data]


def _ports_to_yaml(ports: list[dict]) -> str:
    """Dump ports to clean, round-trippable YAML (ordered keys, on/off quoted)."""
    export = [
        {key: port[key] for key in _PORT_EXPORT_KEYS if port.get(key) not in (None, "")}
        for port in ports
    ]
    return yaml.safe_dump(
        export, sort_keys=False, allow_unicode=True, default_flow_style=False
    )


def _import_schema(mode: str, value: object = None) -> vol.Schema:
    """Merge/replace mode + a YAML editor (ObjectSelector) for the port list.

    ObjectSelector renders HA's YAML code editor (top-aligned, monospace) instead
    of a multiline text field — and hands back the already-parsed list.
    """
    return vol.Schema(
        {
            vol.Required(CONF_IMPORT_MODE, default=mode): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[IMPORT_MODE_MERGE, IMPORT_MODE_REPLACE],
                    mode=selector.SelectSelectorMode.LIST,
                    translation_key="import_mode",
                )
            ),
            vol.Required(
                CONF_PORTS_YAML, description={"suggested_value": value}
            ): selector.ObjectSelector(),
        }
    )


def _coerce_ports(raw: object) -> list[dict]:
    """Accept a parsed list (ObjectSelector) or a YAML string; validate each port."""
    if raw is None or raw == "":
        raise ValueError("no ports given")
    if isinstance(raw, str):
        return _parse_ports_yaml(raw)
    if isinstance(raw, list):
        return [_normalize_imported_port(port) for port in raw]
    raise ValueError("expected a list of ports")


def _export_select_schema(ports: list[dict]) -> vol.Schema:
    """Multi-select of which ports to export (all pre-selected)."""
    options = [
        {"value": str(i), "label": p.get(CONF_LABEL) or f"Port {i + 1}"}
        for i, p in enumerate(ports)
    ]
    return vol.Schema(
        {
            vol.Required(
                CONF_PORT_SELECTION, default=[o["value"] for o in options]
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            )
        }
    )


class NecromancerOptionsFlow(OptionsFlow):
    """Manage the flat list of PoE ports shared by every poe_port guard.

    `init` is a real-button menu showing the current ports plus add / edit /
    delete; edit & delete first pick a port (radio) then return to the menu.
    "Save" writes the list to `entry.options` (closing the dialog discards). The
    edit form reuses the `add_port` step_id so the frontend routes its submit
    there; `_editing` decides replace vs append.
    """

    def __init__(self) -> None:
        self._ports: list[dict] = []
        self._loaded = False
        self._edit_index = 0
        self._editing = False
        self._export_text = ""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not self._loaded:
            self._loaded = True
            self._ports = list(self.config_entry.options.get(CONF_PORTS, []))
        options = ["add_port"]
        if self._ports:
            options += ["edit_port", "delete_port"]
        options.append("import_ports")
        if self._ports:
            options.append("export_ports")
        options.append("save")
        port_list = (
            "\n".join(
                f"{i + 1}. {p.get(CONF_LABEL) or '?'}"
                for i, p in enumerate(self._ports)
            )
            or "—"
        )
        return self.async_show_menu(
            step_id="init",
            menu_options=options,
            description_placeholders={"ports": port_list},
        )

    async def async_step_add_port(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            port = _flatten_sections(user_input)
            if self._editing and 0 <= self._edit_index < len(self._ports):
                self._ports[self._edit_index] = port
            else:
                self._ports.append(port)
            self._editing = False
            return await self.async_step_init()
        current = self._ports[self._edit_index] if self._editing else {}
        return self.async_show_form(
            step_id="add_port",
            data_schema=_port_schema(current, exclude=_own_entities(self.hass)),
        )

    async def async_step_edit_port(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._edit_index = int(user_input["port"])
            self._editing = True
            return await self.async_step_add_port()
        return self.async_show_form(
            step_id="edit_port", data_schema=_port_select_schema(self._ports)
        )

    async def async_step_delete_port(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            index = int(user_input["port"])
            if 0 <= index < len(self._ports):
                self._ports.pop(index)
            return await self.async_step_init()
        return self.async_show_form(
            step_id="delete_port", data_schema=_port_select_schema(self._ports)
        )

    async def async_step_import_ports(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        value: object = None
        mode = IMPORT_MODE_MERGE
        detail = ""
        if user_input is not None:
            value = user_input.get(CONF_PORTS_YAML)
            mode = user_input.get(CONF_IMPORT_MODE, IMPORT_MODE_MERGE)
            try:
                imported = _coerce_ports(value)
            except ValueError as err:
                errors["base"] = "import_failed"
                detail = str(err)
            else:
                if mode == IMPORT_MODE_REPLACE:
                    self._ports = imported
                else:
                    self._merge_ports(imported)
                return await self.async_step_init()
        return self.async_show_form(
            step_id="import_ports",
            data_schema=_import_schema(mode, value),
            errors=errors,
            description_placeholders={"error": detail},
        )

    def _merge_ports(self, imported: list[dict]) -> None:
        """Upsert imported ports into the current list, keyed by label."""
        index_by_label = {p[CONF_LABEL]: i for i, p in enumerate(self._ports)}
        for port in imported:
            existing = index_by_label.get(port[CONF_LABEL])
            if existing is not None:
                self._ports[existing] = port
            else:
                index_by_label[port[CONF_LABEL]] = len(self._ports)
                self._ports.append(port)

    async def async_step_export_ports(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            chosen: list[dict] = []
            for raw in user_input.get(CONF_PORT_SELECTION, []):
                index = int(raw)
                if 0 <= index < len(self._ports):
                    chosen.append(self._ports[index])
            self._export_text = _ports_to_yaml(chosen) if chosen else ""
            return await self.async_step_export_result()
        return self.async_show_form(
            step_id="export_ports", data_schema=_export_select_schema(self._ports)
        )

    async def async_step_export_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_init()
        # Show the YAML as a markdown code block in the description (clean,
        # top-aligned, copyable) rather than a multiline text field, which
        # renders a long prefilled value oddly (vertically centred).
        return self.async_show_form(
            step_id="export_result",
            data_schema=vol.Schema({}),
            description_placeholders={"yaml": self._export_text},
        )

    async def async_step_save(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_create_entry(data={CONF_PORTS: self._ports})
