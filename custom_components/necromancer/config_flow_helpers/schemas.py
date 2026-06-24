"""Schema + helper builders for the Necromancer config/subentry/options flow.

Pure functions that build voluptuous schemas, flatten/lift form sections, derive
form defaults from stored data, build the stored data shape, and import/export the
PoE port list. No flow state — the flow handler classes in `config_flow.py` call
these.
"""

from __future__ import annotations

import voluptuous as vol
import yaml

from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import section
from homeassistant.helpers import entity_registry as er, selector

from ..const import (
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
    CONF_NOTIFY_ACTION,
    CONF_NOTIFY_FOLLOWER_SUCCESS,
    CONF_OFF_ACTION,
    CONF_OFF_ON_DELAY,
    CONF_OFF_TIMEOUT,
    CONF_OFF_VALUE,
    CONF_ON_ACTION,
    CONF_ON_TIMEOUT,
    CONF_ON_VALUE,
    CONF_POLICY,
    CONF_PORT_SELECTION,
    CONF_PORTS_YAML,
    CONF_RELOAD_DELAY,
    CONF_RELOAD_ENTRY,
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
    DEFAULT_RELOAD_DELAY,
    DOMAIN,
    IMPORT_MODE_MERGE,
    IMPORT_MODE_REPLACE,
    MODE_NOTIFY,
    SOURCE_STATE,
    SOURCE_TEMPLATE,
    STRATEGY_ACTION,
    STRATEGY_ACTIONS,
    STRATEGY_POE,
    STRATEGY_SWITCH,
)
from .selectors import (
    _ATTRIBUTE_SELECTOR,
    _HEALTH_VALUE_SELECTOR,
    _ID_ATTRIBUTE,
    _STATUS_ATTRIBUTE,
    _STATUS_VALUE_SELECTOR,
    _entity_selector,
    _seconds_selector,
)

# Every recovery strategy offered in the wizard, in display order. The health-check
# is not a separate strategy — it's a per-recovery toggle in the behaviour section
# (see `_behavior_section`), defaulting on.
_STRATEGIES = [
    STRATEGY_SWITCH,
    STRATEGY_ACTION,
    STRATEGY_ACTIONS,
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


def _as_list(value: object) -> list:
    """A stored value may be a bare string (legacy) or a list; return a list."""
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


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
SECTION_LINK = "linked_guards"
SECTION_BEHAVIOR = "behavior"
SECTION_NOTIFY = "notification"
SECTION_RECOVERY = "recovery_action"
SECTION_POWER = "power"
SECTION_IDENTITY = "identity"
SECTION_STATUS = "status"
SECTION_TIMING = "timing"
SECTION_RELOAD = "reload"


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
    """Return all Necromancer-owned entities (never power-cycle a view-entity)."""
    ent_reg = er.async_get(hass)
    return [e.entity_id for e in ent_reg.entities.values() if e.platform == DOMAIN]


def _own_guard_entities(hass: HomeAssistant, subentry_id: str | None) -> list[str]:
    """Return just THIS guard's own view-entities (for the health-picker exclusion).

    Excluded from its **health** picker so a self-loop can't be picked, while OTHER
    guards' status/health entities stay selectable (enables supervisor / staged
    guards). Empty while adding a new guard (no subentry yet).
    """
    if not subentry_id:
        return []
    ent_reg = er.async_get(hass)
    return [
        e.entity_id
        for e in ent_reg.entities.values()
        if e.platform == DOMAIN and e.unique_id.startswith(subentry_id)
    ]


def _source_schema(default: str) -> vol.Schema:
    """Build the health-source-type selection schema (entity state vs template)."""
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
    """Derive the source type (template vs state) from a stored guard's health."""
    is_template = data.get(CONF_HEALTH, {}).get(CONF_TYPE) == "template"
    return SOURCE_TEMPLATE if is_template else SOURCE_STATE


def _health_fields(d: dict, *, source_type: str, exclude: list[str]) -> dict:
    """The state-detection fields (flat, no section), per the chosen source type."""
    if source_type == SOURCE_TEMPLATE:
        return {
            vol.Required(
                CONF_TEMPLATE, default=d.get(CONF_TEMPLATE, "")
            ): selector.TemplateSelector()
        }
    return {
        vol.Required(
            CONF_ENTITY_ID, default=d.get(CONF_ENTITY_ID, vol.UNDEFINED)
        ): _entity_selector(list(exclude)),
        **_watch_fields(d),
    }


def _device_schema(
    d: dict | None = None, *, source_type: str = SOURCE_STATE, exclude: list[str] = ()
) -> vol.Schema:
    """Build the device step schema: name + health fields + assigned device (flat)."""
    d = d or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=d.get(CONF_NAME, "")): str,
            **_health_fields(d, source_type=source_type, exclude=list(exclude)),
            vol.Optional(
                CONF_DEVICE_ID,
                description={"suggested_value": d.get(CONF_DEVICE_ID)},
            ): selector.DeviceSelector(),
        }
    )


def _strategy_schema(default: str) -> vol.Schema:
    """Build the recovery-strategy selection schema (notify-only + strategies)."""
    return vol.Schema(
        {
            vol.Required(CONF_STRATEGY, default=default): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[MODE_NOTIFY, *_STRATEGIES],
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


def _link_section(
    options: list[dict], default: list[str], *, notify_success: bool = False
) -> dict:
    """Build the collapsed 'linked guards' multi-select section.

    A multi-select of group partners (other recover guards) plus the 'notify the
    follower's success' toggle. Returns an empty dict when there are no other
    recover guards to link to, so the section is simply omitted (an empty
    SelectSelector would be pointless).
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
            ),
            vol.Required(
                CONF_NOTIFY_FOLLOWER_SUCCESS, default=notify_success
            ): selector.BooleanSelector(),
        },
        collapsed=True,
    )


def _reload_section(d: dict) -> dict:
    """Optional 'reload the assigned device's integration after a repair' toggle.

    Only appended (by the flow) when a device is assigned. `d` is the stored
    behavior block, so a reconfigure pre-fills the current values.
    """
    return _section(
        SECTION_RELOAD,
        {
            vol.Required(
                CONF_RELOAD_ENTRY, default=d.get(CONF_RELOAD_ENTRY, False)
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_RELOAD_DELAY,
                default=d.get(CONF_RELOAD_DELAY, DEFAULT_RELOAD_DELAY),
            ): _seconds_selector(600),
        },
    )


def _debounce_field(d: dict) -> dict:
    """Build the debounce-seconds field (how long down before acting)."""
    return {
        vol.Required(
            CONF_DEBOUNCE, default=d.get(CONF_DEBOUNCE, DEFAULT_DEBOUNCE)
        ): _seconds_selector(3600)
    }


def _behavior_section(d: dict) -> dict:
    """Timing/retry behaviour, in a titled section (good defaults).

    Field order is deliberate: debounce + cooldown first, then the `health_check`
    toggle directly above the two fields it governs (boot window = how long to wait
    for the device to read healthy, retries) — so it's visually clear what the
    checkbox controls. Like HA core's birth/will fields, those two stay visible and
    editable even when the check is off; they simply don't apply then. The toggle
    is shown for every strategy (PoE included — it gates the engine's device-health
    VERIFY; the PoE driver's own "port came back" check runs regardless).
    """
    fields: dict = {
        **_debounce_field(d),
        vol.Required(
            CONF_COOLDOWN, default=d.get(CONF_COOLDOWN, DEFAULT_COOLDOWN)
        ): _seconds_selector(86400),
        vol.Required(
            CONF_HEALTH_CHECK, default=d.get(CONF_HEALTH_CHECK, True)
        ): selector.BooleanSelector(),
        vol.Required(
            CONF_BOOT_WINDOW, default=d.get(CONF_BOOT_WINDOW, DEFAULT_BOOT_WINDOW)
        ): _seconds_selector(3600),
        vol.Required(
            CONF_MAX_ATTEMPTS, default=d.get(CONF_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=10, mode=selector.NumberSelectorMode.BOX
            )
        ),
    }
    # Auto-recovery is not a setup field: it's the per-guard runtime switch entity
    # (persisted), so guards start with it on (DEFAULT_AUTO_RESTART).
    return _section(SECTION_BEHAVIOR, fields)


def _switch_fields(d: dict, exclude: list[str]) -> dict:
    """Build the switch-entity + off/on delay fields for the switch strategy."""
    return {
        vol.Required(
            CONF_SWITCH_ENTITY, default=d.get(CONF_SWITCH_ENTITY, vol.UNDEFINED)
        ): _entity_selector(exclude, _SWITCH_DOMAINS),
        vol.Required(
            CONF_OFF_ON_DELAY, default=d.get(CONF_OFF_ON_DELAY, DEFAULT_OFF_ON_DELAY)
        ): _seconds_selector(600),
    }


def _switch_schema(
    d: dict | None = None, *, exclude: list[str] = (), reload_block=None
) -> vol.Schema:
    """Build the switch power-cycle recovery form schema."""
    d = d or {}
    return vol.Schema(
        {
            **_switch_fields(d, list(exclude)),
            **_behavior_section(d),
            **(reload_block or {}),
            **_notification_section(d),
        }
    )


def _action_schema(d: dict | None = None, *, reload_block=None) -> vol.Schema:
    """One recovery action sequence (in its own section) + behaviour."""
    d = d or {}
    return vol.Schema(
        {
            **_section(
                SECTION_RECOVERY,
                {
                    vol.Optional(
                        CONF_ACTION,
                        description={"suggested_value": d.get(CONF_ACTION)},
                    ): selector.ActionSelector(),
                },
            ),
            **_behavior_section(d),
            **(reload_block or {}),
            **_notification_section(d),
        }
    )


def _actions_schema(d: dict | None = None, *, reload_block=None) -> vol.Schema:
    """An "off" and an "on" action sequence + delay, in their own section."""
    d = d or {}
    return vol.Schema(
        {
            **_section(
                SECTION_RECOVERY,
                {
                    vol.Optional(
                        CONF_OFF_ACTION,
                        description={"suggested_value": d.get(CONF_OFF_ACTION)},
                    ): selector.ActionSelector(),
                    vol.Optional(
                        CONF_ON_ACTION,
                        description={"suggested_value": d.get(CONF_ON_ACTION)},
                    ): selector.ActionSelector(),
                    vol.Required(
                        CONF_OFF_ON_DELAY,
                        default=d.get(CONF_OFF_ON_DELAY, DEFAULT_OFF_ON_DELAY),
                    ): _seconds_selector(600),
                },
            ),
            **_behavior_section(d),
            **(reload_block or {}),
            **_notification_section(d),
        }
    )


def _notify_schema(d: dict | None = None) -> vol.Schema:
    """Build the notify-only form schema (debounce + alert action)."""
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
    """Build the stored driver block for the chosen recovery strategy."""
    if strategy == STRATEGY_POE:
        return {CONF_TYPE: "poe_port", CONF_EXPECTED_ID: step2[CONF_EXPECTED_ID]}
    if strategy == STRATEGY_ACTION:
        return {CONF_TYPE: "action_call", CONF_ACTION: step2.get(CONF_ACTION)}
    if strategy == STRATEGY_ACTIONS:
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
    """Assemble the full stored subentry data from the two wizard steps."""
    step1 = _flatten_sections(step1)
    step2 = _flatten_sections(step2)
    notify_only = strategy == MODE_NOTIFY
    # Health-check is a per-recovery toggle (in step2), shown for every strategy.
    check = bool(step2.get(CONF_HEALTH_CHECK, True))
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
        # The numbers are always in the form (editable even when the check is off,
        # like HA's birth/will fields); store them so toggling back keeps them.
        behavior[CONF_BOOT_WINDOW] = int(step2[CONF_BOOT_WINDOW])
        behavior[CONF_MAX_ATTEMPTS] = int(step2[CONF_MAX_ATTEMPTS])
        data[CONF_DRIVER] = _build_driver(step2, strategy)
        if step1.get(CONF_DEVICE_ID) and step2.get(CONF_RELOAD_ENTRY):
            behavior[CONF_RELOAD_ENTRY] = True
            behavior[CONF_RELOAD_DELAY] = int(
                step2.get(CONF_RELOAD_DELAY, DEFAULT_RELOAD_DELAY)
            )
    if step1.get(CONF_DEVICE_ID):
        data[CONF_DEVICE_ID] = step1[CONF_DEVICE_ID]
    if not notify_only and step2.get(CONF_LINKED_GUARDS):
        data[CONF_LINKED_GUARDS] = sorted(step2[CONF_LINKED_GUARDS])
    if not notify_only and step2.get(CONF_NOTIFY_FOLLOWER_SUCCESS):
        behavior[CONF_NOTIFY_FOLLOWER_SUCCESS] = True
    return data


def _current_strategy(data: dict) -> str:
    """Derive the wizard strategy key from a stored guard's driver type.

    The health-check is no longer part of the strategy (it's a per-recovery
    toggle), so the driver type alone determines the strategy.
    """
    if data.get(CONF_POLICY, {}).get(CONF_TYPE) == MODE_NOTIFY:
        return MODE_NOTIFY
    dtype = data.get(CONF_DRIVER, {}).get(CONF_TYPE)
    if dtype == "poe_port":
        return STRATEGY_POE
    if dtype == "action_cycle":
        return STRATEGY_ACTIONS
    if dtype == "action_call":
        return STRATEGY_ACTION
    return STRATEGY_SWITCH


def _watch_defaults(block: dict) -> dict:
    """Flatten a stored health/verify block into _watch_fields defaults."""
    source = block.get(CONF_SOURCE, "state")
    return {
        CONF_ATTRIBUTE: None if source == "state" else source,
        CONF_ON_VALUE: block.get(CONF_ON_VALUE) or block.get(CONF_HEALTHY_STATE, "on"),
        CONF_OFF_VALUE: block.get(CONF_OFF_VALUE, "off"),
    }


def _health_defaults(data: dict) -> dict:
    """Pre-fill the device step from a stored guard (name + health + device)."""
    health = data.get(CONF_HEALTH, {})
    return {
        CONF_NAME: data.get(CONF_NAME, ""),
        CONF_ENTITY_ID: health.get(CONF_ENTITY_ID),
        CONF_TEMPLATE: health.get(CONF_TEMPLATE, ""),
        **_watch_defaults(health),
        CONF_DEVICE_ID: data.get(CONF_DEVICE_ID),
    }


def _behavior_defaults(data: dict) -> dict:
    """Pre-fill the timing/retry/notify fields from a stored guard's behavior."""
    b = data.get(CONF_BEHAVIOR, {})
    return {
        CONF_DEBOUNCE: b.get(CONF_DEBOUNCE, DEFAULT_DEBOUNCE),
        CONF_HEALTH_CHECK: b.get(CONF_HEALTH_CHECK, True),
        CONF_BOOT_WINDOW: b.get(CONF_BOOT_WINDOW, DEFAULT_BOOT_WINDOW),
        CONF_COOLDOWN: b.get(CONF_COOLDOWN, DEFAULT_COOLDOWN),
        CONF_MAX_ATTEMPTS: b.get(CONF_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS),
        CONF_NOTIFY_ACTION: b.get(CONF_NOTIFY_ACTION),
    }


def _switch_defaults(data: dict) -> dict:
    """Pre-fill the switch strategy form from a stored guard."""
    drv = data.get(CONF_DRIVER, {})
    return {
        **_behavior_defaults(data),
        CONF_SWITCH_ENTITY: drv.get(CONF_SWITCH_ENTITY, vol.UNDEFINED),
        CONF_OFF_ON_DELAY: drv.get(CONF_OFF_ON_DELAY, DEFAULT_OFF_ON_DELAY),
    }


def _action_defaults(data: dict) -> dict:
    """Pre-fill the single-action strategy form from a stored guard."""
    return {
        **_behavior_defaults(data),
        CONF_ACTION: data.get(CONF_DRIVER, {}).get(CONF_ACTION),
    }


def _actions_defaults(data: dict) -> dict:
    """Pre-fill the off/on action-pair strategy form from a stored guard."""
    drv = data.get(CONF_DRIVER, {})
    return {
        **_behavior_defaults(data),
        CONF_OFF_ACTION: drv.get(CONF_OFF_ACTION),
        CONF_ON_ACTION: drv.get(CONF_ON_ACTION),
        CONF_OFF_ON_DELAY: drv.get(CONF_OFF_ON_DELAY, DEFAULT_OFF_ON_DELAY),
    }


def _poe_defaults(data: dict) -> dict:
    """Pre-fill the PoE-port strategy form from a stored guard."""
    drv = data.get(CONF_DRIVER, {})
    return {
        **_behavior_defaults(data),
        CONF_EXPECTED_ID: drv.get(CONF_EXPECTED_ID, ""),
    }


def _poe_schema(d: dict | None = None, *, reload_block=None) -> vol.Schema:
    """Build the PoE-port recovery form schema (expected id + behaviour)."""
    d = d or {}
    return vol.Schema(
        {
            vol.Required(CONF_EXPECTED_ID, default=d.get(CONF_EXPECTED_ID, "")): str,
            **_behavior_section(d),
            **(reload_block or {}),
            **_notification_section(d),
        }
    )


# ---------- search-area ports ----------


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


def _validate_port_identity(port: dict) -> tuple[str, str] | None:
    """Reject a port that identifies its device two ways at once.

    A port's id comes from EITHER an entity (its state / a chosen attribute) OR a
    typed-in value — never both. Returns (field, error_key) for the config flow,
    or None when the identity is valid.
    """
    if port.get(CONF_ID_STATIC) and port.get(CONF_ID_ENTITY):
        return CONF_ID_STATIC, "id_conflict"
    if port.get(CONF_ID_ATTRIBUTE) and not port.get(CONF_ID_ENTITY):
        return CONF_ID_ATTRIBUTE, "attribute_needs_entity"
    return None


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
    if conflict := _validate_port_identity(port):
        if conflict[1] == "id_conflict":
            raise ValueError(
                f"port '{label}' identifies its device twice — use an entity or a "
                "static label, not both"
            )
        raise ValueError(f"port '{label}' has an id attribute but no id entity")
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
