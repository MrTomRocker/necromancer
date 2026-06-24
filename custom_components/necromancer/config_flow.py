"""Config + subentry + options flow for Necromancer.

The integration is a **single service** entry (added once, blank). Every guarded
device is a config **subentry** of type `device`, added via "Add device" and
edited via its "Reconfigure" button.

The strategy step offers five choices: notify-only (just observe) plus four
recovery strategies — power-cycle a `switch`, run one `action` sequence, an off/on
pair of `actions`, and `poe_port` (auto-resolve the device to a PoE port by id).
Whether recovery is verified against the device's health entity (the engine's
VERIFY step) is a per-recovery `health_check` toggle in the behaviour section,
defaulting on, shown for every strategy.

The health "what to watch" block — entity + attribute (empty = state) + on/off
values — lives in the device step. Every guard is device & health → strategy →
final step (a recovery form, or the notify form when notify-only is picked).

PoE ports are a single **flat list** managed via the service's **options flow**
(add / edit / delete port). Every `poe_port` guard searches that whole list by
its `expected_id`; there is no per-area grouping.

This file stays a single module at the integration root (hassfest requires
`config_flow.py` to be a file); the schema/selector helpers it uses live in the
`config_flow_helpers` package.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr

from .config_flow_helpers.schemas import (
    _action_defaults,
    _action_schema,
    _actions_defaults,
    _actions_schema,
    _behavior_defaults,
    _build_data,
    _coerce_ports,
    _current_strategy,
    _device_schema,
    _export_select_schema,
    _flatten_sections,
    _health_defaults,
    _import_schema,
    _link_section,
    _notify_schema,
    _own_entities,
    _own_guard_entities,
    _poe_defaults,
    _poe_schema,
    _port_schema,
    _port_select_schema,
    _ports_to_yaml,
    _reload_section,
    _source_schema,
    _source_type_of,
    _strategy_schema,
    _switch_defaults,
    _switch_schema,
    _validate_port_identity,
)
from .const import (
    CONF_ACTION,
    CONF_BEHAVIOR,
    CONF_DEVICE_ID,
    CONF_IMPORT_MODE,
    CONF_LABEL,
    CONF_LINKED_GUARDS,
    CONF_NOTIFY_FOLLOWER_SUCCESS,
    CONF_OFF_ACTION,
    CONF_ON_ACTION,
    CONF_POLICY,
    CONF_PORT_SELECTION,
    CONF_PORTS,
    CONF_PORTS_YAML,
    CONF_SOURCE_TYPE,
    CONF_STRATEGY,
    CONF_TYPE,
    DOMAIN,
    IMPORT_MODE_MERGE,
    IMPORT_MODE_REPLACE,
    MODE_NOTIFY,
    SOURCE_STATE,
    STRATEGY_ACTION,
    STRATEGY_ACTIONS,
    STRATEGY_POE,
    STRATEGY_SWITCH,
    SUBENTRY_TYPE_DEVICE,
)
from .core.links import group_of

LOGGER = logging.getLogger(__name__)


class NecromancerConfigFlow(ConfigFlow, domain=DOMAIN):
    """A single blank service entry.

    Guarded devices are `device` subentries; PoE ports are a flat list in the
    entry's options (the options flow).
    """

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the single blank service entry (one per install)."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        return self.async_create_entry(title="Necromancer", data={})

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Expose the `device` subentry flow used to add guarded devices."""
        return {SUBENTRY_TYPE_DEVICE: DeviceSubentryFlow}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow that manages the flat PoE port list."""
        return NecromancerOptionsFlow()


class DeviceSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure one guarded device. Add and reconfigure share steps."""

    def __init__(self) -> None:
        """Init the device subentry flow with add-mode defaults."""
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
        """Return the stored data of the subentry being reconfigured."""
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
        """Return the edited guard's subentry id, or None while adding."""
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
        notify_success = (
            self._reconfig_data()
            .get(CONF_BEHAVIOR, {})
            .get(CONF_NOTIFY_FOLLOWER_SUCCESS, False)
            if self._reconfig
            else False
        )
        section_dict = _link_section(
            self._link_options(), self._linked_default(), notify_success=notify_success
        )
        return schema.extend(section_dict) if section_dict else schema

    def _reload_block(self) -> dict:
        """Build the optional 'reload assigned device integration' section.

        Only present when a device was set in the device step (nothing to reload
        otherwise). Inserted before the notification section by the strategy schema.
        """
        if not self._step1.get(CONF_DEVICE_ID):
            return {}
        d = self._reconfig_data().get(CONF_BEHAVIOR, {}) if self._reconfig else {}
        return _reload_section(d)

    # ---------- source type (entity state vs template) ----------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Start adding a guard: pick the health source type (state vs template)."""
        return await self._source(user_input, reconfig=False)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Start reconfiguring a guard: pick the health source type."""
        return await self._source(user_input, reconfig=True)

    async def _source(
        self, user_input: dict[str, Any] | None, *, reconfig: bool
    ) -> SubentryFlowResult:
        """Show the source-type step, then advance to the device step."""
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
        """Collect name, health (what to watch) and the optional assigned device."""
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
                # Only THIS guard's own entities — other guards' status/health stay
                # pickable so you can build supervisor / staged guards.
                exclude=_own_guard_entities(self.hass, self._own_subentry_id()),
            ),
            errors=errors,
        )

    # ---------- strategy select (recover) ----------
    async def async_step_strategy(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pick the recovery strategy (or notify-only) and route to its form."""
        if user_input is not None:
            self._strategy = user_input[CONF_STRATEGY]
            return await {
                MODE_NOTIFY: self.async_step_notify,
                STRATEGY_SWITCH: self.async_step_switch,
                STRATEGY_ACTION: self.async_step_action,
                STRATEGY_ACTIONS: self.async_step_actions,
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

    # ---------- recovery strategy forms (one step per action shape) ----------
    async def async_step_switch(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show the power-cycle-a-switch recovery form."""
        if user_input is not None:
            return await self._finish(
                _build_data(self._step1, user_input, self._strategy)
            )
        d = _switch_defaults(self._reconfig_data()) if self._reconfig else None
        return self.async_show_form(
            step_id="switch",
            data_schema=self._with_link(
                _switch_schema(
                    d,
                    exclude=_own_entities(self.hass),
                    reload_block=self._reload_block(),
                )
            ),
        )

    async def async_step_action(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show the single-action-sequence recovery form."""
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
            data_schema=self._with_link(
                _action_schema(d, reload_block=self._reload_block())
            ),
            errors=errors,
        )

    async def async_step_actions(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show the off/on action-pair recovery form."""
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
            data_schema=self._with_link(
                _actions_schema(d, reload_block=self._reload_block())
            ),
            errors=errors,
        )

    # ---------- poe_port (auto-resolve against the flat port list) ----------
    async def async_step_poe_port(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show the PoE-port recovery form (auto-resolve by expected id)."""
        if user_input is not None:
            return await self._finish(
                _build_data(self._step1, user_input, STRATEGY_POE)
            )
        d = _poe_defaults(self._reconfig_data()) if self._reconfig else None
        return self.async_show_form(
            step_id="poe_port",
            data_schema=self._with_link(
                _poe_schema(d, reload_block=self._reload_block())
            ),
        )

    # ---------- notify-only ----------
    async def async_step_notify(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show the notify-only form (observe, never recover)."""
        if user_input is not None:
            return await self._finish(_build_data(self._step1, user_input, MODE_NOTIFY))
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
        """Create the guard subentry, or update and abort on reconfigure."""
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


class NecromancerOptionsFlow(OptionsFlow):
    """Manage the flat list of PoE ports shared by every poe_port guard.

    `init` is a real-button menu showing the current ports plus add / edit /
    delete; edit & delete first pick a port (radio) then return to the menu.
    "Save" writes the list to `entry.options` (closing the dialog discards). The
    edit form reuses the `add_port` step_id so the frontend routes its submit
    there; `_editing` decides replace vs append.
    """

    def __init__(self) -> None:
        """Init the options flow (ports loaded lazily on first step)."""
        self._ports: list[dict] = []
        self._loaded = False
        self._edit_index = 0
        self._editing = False
        self._export_text = ""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the PoE port menu (add / edit / delete / import / export / save)."""
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
        """Show the port form; append a new port or replace the one being edited."""
        errors: dict[str, str] = {}
        if user_input is not None:
            port = _flatten_sections(user_input)
            if conflict := _validate_port_identity(port):
                errors[conflict[0]] = conflict[1]
            else:
                if self._editing and 0 <= self._edit_index < len(self._ports):
                    self._ports[self._edit_index] = port
                else:
                    self._ports.append(port)
                self._editing = False
                return await self.async_step_init()
            current = port
        else:
            current = self._ports[self._edit_index] if self._editing else {}
        return self.async_show_form(
            step_id="add_port",
            data_schema=_port_schema(current, exclude=_own_entities(self.hass)),
            errors=errors,
        )

    async def async_step_edit_port(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a port to edit, then open the port form pre-filled with it."""
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
        """Pick a port to delete, drop it, and return to the menu."""
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
        """Import ports from pasted YAML, merging into or replacing the list."""
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
                LOGGER.debug("Importing %s PoE port(s) (%s)", len(imported), mode)
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
        """Pick which ports to export, then render them as YAML."""
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
        """Show the exported YAML as a copyable code block, then return to menu."""
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
        """Write the working port list to the entry options."""
        LOGGER.debug("Saving %s PoE port(s)", len(self._ports))
        return self.async_create_entry(data={CONF_PORTS: self._ports})
