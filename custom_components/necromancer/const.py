"""Constants for the Necromancer integration."""

from __future__ import annotations

import logging

LOGGER = logging.getLogger(__package__)

DOMAIN = "necromancer"

# Each guarded device is a config subentry of the single service entry.
SUBENTRY_TYPE_DEVICE = "device"

# Runtime-state persistence (entity-independent; entities are display only).
STORAGE_VERSION = 1
SAVE_DELAY = 5

# Platforms with entities (Phase 1).
PLATFORMS: list[str] = ["sensor", "binary_sensor", "switch", "button"]

# --- config sections ---
CONF_HEALTH = "health"
CONF_DRIVER = "driver"
CONF_POLICY = "policy"
CONF_BEHAVIOR = "behavior"
CONF_TYPE = "type"

# Wizard: recovery mode → policy + driver type.
CONF_MODE = "mode"
MODE_RECOVER = "recover"
MODE_NOTIFY = "notify"

# Policy gate verdicts (returned by RecoveryPolicy.should_attempt, branched on by
# the engine): observe = notify-only "problem detected"; auto_off = the per-guard
# auto-recovery switch is off.
REASON_OBSERVE = "observe"
REASON_AUTO_OFF = "auto_off"

# device (optional link to an existing HA device)
CONF_DEVICE_ID = "device_id"

# guard linking: other recover-guards this guard is grouped with. When any member
# of the group enters recovery, the others FOLLOW into a hold (no competing
# recovery) and re-validate afterwards. Stored per guard as a list of partner
# subentry_ids; the relation is kept symmetric and clique-closed (a group), so
# linking A-B where B-C exists groups {A,B,C}. The repair lifecycle is also fired
# as an event so external automations can react.
CONF_LINKED_GUARDS = "linked_guards"
EVENT_GUARD_REPAIR = f"{DOMAIN}_guard_repair"

# health source type (wizard step): an entity's state, or a Jinja template that
# evaluates to true/false (continuous → checkable, so verify still works).
CONF_SOURCE_TYPE = "source_type"
SOURCE_STATE = "state_based"
SOURCE_TEMPLATE = "template_based"

# health (entity_state)
CONF_ENTITY_ID = "entity_id"
CONF_HEALTHY_STATE = "healthy_state"

# health (template)
CONF_TEMPLATE = "template"

# recovery strategy (recover mode → which driver, + optional health-check). The
# health-check variants verify recovery against the device's health entity (the
# engine's VERIFY step); the plain ones assume the action worked.
CONF_STRATEGY = "strategy"
STRATEGY_SWITCH = "switch"
STRATEGY_SWITCH_CHECK = "switch_check"
STRATEGY_ACTION = "action"
STRATEGY_ACTION_CHECK = "action_check"
STRATEGY_ACTIONS = "actions"
STRATEGY_ACTIONS_CHECK = "actions_check"
STRATEGY_POE = "poe_port"

# recovery action sequences: action_call = one action; action_cycle = off + on
CONF_ACTION = "action"
CONF_OFF_ACTION = "off_action"
CONF_ON_ACTION = "on_action"

# driver (poe_port): resolve device -> port from the flat port list, then cycle +
# verify. The port list lives in the service entry's options (managed via the options
# flow); every poe_port guard searches the whole list by its expected_id.
CONF_EXPECTED_ID = "expected_id"
CONF_PORTS = "ports"
# a port in the flat port list
CONF_LABEL = "label"
CONF_ACTUATOR = "actuator"
CONF_ID_ENTITY = "id_entity"
CONF_ID_ATTRIBUTE = "id_attribute"
CONF_ID_STATIC = "id_static"
CONF_STATUS_ENTITY = "status_entity"
CONF_STATUS_ATTRIBUTE = "status_attribute"
CONF_STATUS_ON = "status_on"
CONF_STATUS_OFF = "status_off"
DEFAULT_PORT_OFF_TIMEOUT = 20
DEFAULT_PORT_ON_TIMEOUT = 60

# options flow: import/export the flat port list as YAML (bulk-edit escape hatch)
CONF_IMPORT_MODE = "import_mode"
IMPORT_MODE_MERGE = "merge"
IMPORT_MODE_REPLACE = "replace"
CONF_PORTS_YAML = "ports_yaml"
CONF_PORT_SELECTION = "selection"

# port-level repair service (necromancer.repair_poe_port) — the shared primitive
# guards (and other automations) call to power-cycle a port by device id.
SERVICE_REPAIR_POE_PORT = "repair_poe_port"
ATTR_ID = "id"

# driver (switch_cycle): power-cycle a switch (off → delay → on)
CONF_SWITCH_ENTITY = "switch_entity"
CONF_OFF_ON_DELAY = "off_on_delay"

# "what to watch" block — shared by health (entity_state) and poe-port status
CONF_SOURCE = "source"
CONF_ATTRIBUTE = "attribute"
CONF_OFF_VALUE = "off_value"
CONF_ON_VALUE = "on_value"
CONF_OFF_TIMEOUT = "off_timeout"
CONF_ON_TIMEOUT = "on_timeout"

# behavior
CONF_DEBOUNCE = "debounce"
CONF_BOOT_WINDOW = "boot_window"
CONF_COOLDOWN = "cooldown"
CONF_MAX_ATTEMPTS = "max_attempts"
CONF_AUTO_RESTART = "auto_restart"
# Verify recovery against the device's health entity (engine VERIFY step)?
# False = assume the action worked (continuous monitoring re-triggers if not).
CONF_HEALTH_CHECK = "health_check"
# What to run when something happens (problem/recovery/escalation): a user-defined
# action sequence (script syntax) instead of fixed notify entities — the user
# decides whether/how to notify. Variables: message, name, event, + event params.
CONF_NOTIFY_ACTION = "notify_action"
# After a repair attempt, reload the assigned device's integration (its config
# entry) before VERIFY — only offered when a device is assigned. The delay gives
# the just-repaired device time to come up before HA reconnects to it.
CONF_RELOAD_ENTRY = "reload_entry"
CONF_RELOAD_DELAY = "reload_delay"
# A follower that recovers by following a group repair is silent on success by
# default (one root-cause repair -> one notification, the leader's). Opt in per
# guard to also notify the follower's success. Failures always notify.
CONF_NOTIFY_FOLLOWER_SUCCESS = "notify_follower_success"

# defaults (seconds, except counts/bools)
DEFAULT_DEBOUNCE = 120
DEFAULT_BOOT_WINDOW = 180
DEFAULT_COOLDOWN = 600
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_AUTO_RESTART = True
DEFAULT_HEALTHY_STATE = "on"
DEFAULT_OFF_ON_DELAY = 5
DEFAULT_RELOAD_DELAY = 10

# User-facing notification message templates (str.format with name/attempt/max/...).
# Kept in code rather than strings.json because Home Assistant's translation schema
# has no "notify" category; notify.py picks the language with an English fallback.
NOTIFY_MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "recovery_attempt": "{name}: Recovery {attempt}/{max}",
        "recovery_success": "{name}: Recovery succeeded.",
        "recovery_failed": "{name}: Recovery failed after {attempt} attempt(s).",
        "recovery_blocked": "{name}: Recovery blocked — recovery action missing or not callable.",
        "no_auto_recovery": "{name}: Problem detected, auto-recovery is disabled.",
        "problem_detected": "{name}: Problem detected (notify only).",
        "linked_repair_failed": "{name}: Linked repair failed — still faulty.",
    },
    "de": {
        "recovery_attempt": "{name}: Reparatur {attempt}/{max}",
        "recovery_success": "{name}: Reparatur erfolgreich.",
        "recovery_failed": "{name}: Reparatur fehlgeschlagen nach {attempt} Versuchen.",
        "recovery_blocked": "{name}: Reparatur blockiert — Reparatur-Aktion fehlt oder ist nicht aufrufbar.",
        "no_auto_recovery": "{name}: Problem erkannt, Auto-Reparatur ist deaktiviert.",
        "problem_detected": "{name}: Problem erkannt (nur Benachrichtigung).",
        # linked_repair_failed: a follower escalates when the group repair failed.
        "linked_repair_failed": "{name}: Reparatur über verknüpften Guard fehlgeschlagen.",
    },
}
