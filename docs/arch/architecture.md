# Necromancer — Architecture

> A generic **self-healing framework** for Home Assistant: it watches devices,
> decides when one is broken, and runs a recovery — power-cycle a switch, run an
> action, or auto-resolve and reboot a PoE port. It replaces the usual pile of
> bespoke "ping → reload/restart" automations with one configurable engine.

---

## 1. Design philosophy

Necromancer is built around **three pluggable layers**, each with a *generic
escape hatch* so the common case needs no custom code and the rare case is still
expressible:

```
 ┌──────────────┐     ┌──────────────────────────┐     ┌──────────────────┐
 │ HealthSource │ ──▶ │ Engine (RecoveryPolicy)  │ ──▶ │ RecoveryDriver   │
 │  "is it ok?" │     │  state machine + timing  │     │  "fix it"        │
 └──────────────┘     └──────────────────────────┘     └──────────────────┘
```

- **HealthSource** answers *“is this device healthy right now?”* → `OK`,
  `UNHEALTHY`, or `UNKNOWN`. `UNKNOWN` is explicitly **not** unhealthy (no false
  alarms).
- **RecoveryPolicy** is the engine’s strategy: `standard` (recover) or `notify`
  (observe only). It gates whether/when recovery is attempted.
- **RecoveryDriver** performs the actual repair.

The guiding rule: **every edge has a generic strategy; bespoke code only where it
pays off** (the PoE port resolver is the single bespoke driver).

---

## 2. Configuration model & lifecycle

Necromancer is a **single service** config entry (`integration_type: service`, added
once, blank). Everything else hangs off it:

| Thing | Where it lives |
|---|---|
| **Service** | One config entry (`data` blank). |
| **Guarded device** | A config **subentry** of type `device` (one per watched device). Added via *“Add device”*, edited via its *Reconfigure* button. |
| **PoE ports** | A flat list in the service entry’s **options** (`entry.options["ports"]`), managed via the **options flow** (add / edit / delete a port, plus **YAML import / export** for bulk edits). |

One `DeviceEngine` is built per `device` subentry and lives in
`entry.runtime_data` keyed by `subentry_id`. Adding/changing a subentry reloads
the service; an options (ports) change also reloads it, so `poe_port` guards always
see a fresh port list.

There is **no per-area grouping** and no second config entry — an earlier
two-entry split was reverted because the HA frontend can’t filter the subentry
picker by type (it would offer both services for every “Add” button).

### How Necromancer maps onto HA — the object graph

```
ConfigEntry  (one, integration_type: service)      entry.entry_id — stable ULID
├─ runtime_data : NecromancerData                  per-entry, dies on unload
│    └─ engines : { subentry_id → DeviceEngine }   one engine per guarded device
├─ subentries  : { subentry_id → device cfg }      the guarded-device config
├─ options     : { ports: [...] }                  PoE port list (options flow)
└─ Store  .storage/necromancer.<entry_id>          durable truth (survives restart)

hass.data[DOMAIN]["fabric"] : PoeFabric            domain singleton, outlives reloads
```

Each `DeviceEngine` owns its view entities (`sensor` / `binary_sensor` / `switch` /
`button` / `event`). Those entities are **pure view** (§9); the engine + Store hold
the truth (§3). `hass.states` is only a volatile in-RAM snapshot, rebuilt from the
Store on every restart — never the source of truth.

### Lifecycle — what HA calls, and when

| HA calls (by name) | When |
|---|---|
| `async_setup_entry` | entry loaded: boot, first add, **after every reload** — builds all engines, then `async_forward_entry_setups` creates the entities. |
| `async_unload_entry` | entry torn down: shutdown, **before every reload**, removal — flushes the Store, stops engines, unloads platforms. |
| `async_remove_config_entry_device` | user deletes a guarded device in the UI. |
| `_async_reload_entry` (registered update listener) | the entry's options/subentries change — calls `async_reload`. |

**One entry → full rebuild.** Every change (a port option, a device subentry, a
health-entity rename) funnels through the update listener →
`hass.config_entries.async_reload` → `async_unload_entry` **then**
`async_setup_entry`. There is no incremental update: *all* engines are torn down and
rebuilt even when a single device changed. Blunt but correct — the whole
engine/link/port graph depends on the config, and at Necromancer's scale (a handful
of guards) the cost is irrelevant. The `entry_id` is stable across reloads and
restarts; only `subentry_id`s tell you *which* guard changed.

### Runtime updates — entities are push-only

Entities never poll. On any change the engine calls `_emit()`, which invokes each
subscribed entity's `async_write_ha_state`; HA then reads the entity's properties
(`is_on`, `available`, …) and writes a fresh `State` into `hass.states`. One
`_emit()` refreshes a guard's whole entity set at once. (Entity wiring: §9.)

---

## 3. The engine state machine

`core/engine.py` runs a fixed state machine per guard. States
(`sensor.<guard>_status`):

```
        ┌──────────────────────────── healthy again ───────────────────────────┐
        ▼                                                                        │
   ┌────────┐  unhealthy   ┌─────────┐  debounce   ┌────────────┐  driver.recover()
   │   OK   │ ───────────▶ │ SUSPECT │ ──────────▶ │ RECOVERING │ ──────────────┐
   └────────┘              └─────────┘              └────────────┘               │
        ▲                       │ healthy                                        ▼
        │                       └─────────▶ OK                          ┌────────────────┐
        │                                                               │ VERIFY         │
   ┌──────────┐  cooldown over   ┌──────────┐   health OK ◀─────────────│ (boot_window)  │
   │ COOLDOWN │ ◀────────────────│ (success)│                           └────────────────┘
   └──────────┘                  └──────────┘                                  │ timeout
        │ still unhealthy                                                       ▼
        └─────────▶ SUSPECT                                   retry  ┌────────────────────┐
                                                              ◀──────│ attempt < max?     │
                                          ESCALATED ◀── no ─────────│                    │
                                                                     └────────────────────┘
```

**Operator services (off the automatic flow).** Three services act on a guard out-of-band:
`reset` clears `ESCALATED → OK` and re-derives from live health (a manual "try again", no
needless repair if already healthy); `snooze`/`unsnooze` move it to/from the `SNOOZED` state —
health is ignored, no transitions, no alerts (planned maintenance). `snooze` takes a
`duration`, **auto-resumes** when it elapses (the remaining time survives a restart), and is
refused (`ServiceValidationError`) while a recovery cycle is in flight; a snoozed guard also
never follows a linked-group repair. Recovering *now* stays the `button.<guard>_revive`,
arming the `switch.<guard>_auto_recovery`. `reset`/`snooze`/`unsnooze`/`notify_guard` are **entity** services
on the sensor platform (per guard / device / area); `snooze_all` + `unsnooze_all` are
**domain** services (no target, every guard — "maintenance mode"; busy guards skipped
best-effort) registered in `__init__` alongside `repair_poe_port`.

Key timing fields (the **behaviour** block):

| Field | Meaning |
|---|---|
| `debounce` | How long unhealthy before acting (filters blips). |
| `boot_window` | How long to wait for the device to come back (the VERIFY step). |
| `cooldown` | Pause after a recovery cycle before re-arming. |
| `max_attempts` | Retries within one cycle before escalating. |

**Health Check vs fire-and-forget.** `behavior.health_check` (a per-recovery toggle
in the wizard, default on) decides:

- **On**: after `recover()`, the engine waits (event-driven, up to `boot_window`)
  for the HealthSource to read `OK`. Not OK within the window → retry up to
  `max_attempts` → `ESCALATED`.
- **Off**: `recover()` is assumed to have worked → straight to success; the
  continuous health monitoring re-triggers later if it didn’t. *(If `recover()`
  raises — e.g. a missing service — it counts as a failed attempt, never a
  success.)*

**Reload the assigned device's integration (optional).** If a device is assigned
and the guard has `behavior.reload_entry`, then after `recover()` (and before
VERIFY) the engine waits `reload_delay` seconds and reloads the device's config
entry — `device.primary_config_entry` (fallback: all `config_entries`) via
`hass.config_entries.async_reload` (`_maybe_reload_device_entry`). Best-effort: a
missing device or a failing reload is logged but never aborts the cycle — VERIFY
still decides success. Lets HA reconnect to a device that just came back without
scripting a `homeassistant.reload_config_entry` action.

**Persistence.** Runtime state is persisted in a `Store`
(`.storage/necromancer.<entry_id>`), independent of the display entities. Per
guard the engine stores `{state, attempt, recover_count, last_recover, last_seen,
auto, snooze_until}`; alongside those per-guard snapshots the same Store file holds the PoE
fabric's `id → port` cache under a separate `_poe_cache` key (written by
`__init__`'s `_serialize`). On restart the stats and `auto` flag are restored,
`ESCALATED` is restored (then re-derived from live health), `SNOOZED` is restored
(re-arming the *remaining* snooze, or resuming if it elapsed), and transient states
(RECOVERING/COOLDOWN/VERIFY) are *not* restored — they come back as
OK/live-health. The fabric's `_poe_cache` is restored too, so a `poe_port` guard
keeps its last-known fallback port across a reboot. The display entities
(`sensor.*_status`, `binary_sensor.*_health`, `switch.*_auto_recovery`,
`button.*_revive`) are **pure view**; the `Store` is the source of truth.

---

## 4. Health Sources (`core/health/`)

`evaluate() -> Health` is always callable (so the VERIFY step can re-check). A
source that tracks something other than entity states registers its own listener
in `async_setup(on_change)` (returns an unsub) and exposes an empty
`watched_entities`.

| Type | What it is | Healthy when |
|---|---|---|
| `entity_state` | One entity’s state or attribute vs on/off **value lists**. | value ∈ `on_value` → OK; ∈ `off_value` → unhealthy; else `UNKNOWN`. `unavailable`/`unknown` → `UNKNOWN` **unless** listed in `off_value` (explicit off wins). |
| `template` | An inline Jinja template that returns a boolean. | whitelist: `true`/`on`/`1`/`yes` → OK, `false`/`off`/`0`/`no` → UNHEALTHY; anything else (render error, blank, `none`, `unknown`/`unavailable`, unrecognized text) → `UNKNOWN`. |

The **template** source is the inline alternative to building a template *entity*.
Because a template is a continuous, checkable expression (unlike a momentary
*trigger*), it supports the full recover→verify cycle. It is tracked via
`async_track_template_result`, so health re-evaluates whenever a referenced
entity changes.

The wizard’s first step picks the source type (`state_based` / `template_based`).

---

## 5. Recovery drivers (`core/drivers/`)

`recover()` performs the repair; `can_recover()` is a pre-flight guard that blocks
(→ `recovery_blocked`, no blind action) when something is missing.

| Driver | Action | Pre-flight (`can_recover`) |
|---|---|---|
| `switch_cycle` | `turn_off` → `off_on_delay` → `turn_on`. | switch entity exists. |
| `action_call` | Run one user-defined action sequence (script syntax). | action valid & non-empty. |
| `action_cycle` | Run an *off* action → delay → *on* action. | both actions valid. |
| `poe_port` | Resolve the device to a PoE port by `expected_id`, then cycle its actuator with staged status verify. Learns the port while healthy and falls back to the last-known port when the device has aged out. | one live **or** last-known port. |
| `noop` | Nothing (used by notify-only guards). | — |

User actions are validated (`cv.SCRIPT_SCHEMA` + `async_validate_actions_config`)
and run via the `Script` helper (`core/actions.py`), blocking for recovery, detached
for notifications.

**Action variable scope.** `recover(variables)` is handed the engine's run context —
`attempt`, `max`, `name`, `guard_entity_id` (the guard's status sensor, e.g. the
`necromancer.notify_guard` target) — which the action-running drivers (`action_call`,
`action_cycle`) seed into the script run as template variables (`switch_cycle` / `poe_port` /
`noop` ignore it). `async_run` returns the run's **final variable scope** (minus HA's injected
`context`), and `action_cycle` feeds the off action's scope into the on action — so off-phase
`variables:` and the engine context are both readable in the on phase, with no helper entity to
carry state across the power-cycle. Variables are per-attempt (each retry runs off→on fresh);
nothing leaks between attempts. Within a run the scope is one flat namespace (HA
`ScriptRunVariables`, a ChainMap whose writes land in the nearest scope that already defines the
name, else top-level): a `variables:` set inside an `if`/`choose`/`repeat`/`sequence` leaks to the
rest of the run — and on into the on phase — so a *conditionally* set variable is simply `undefined`
when its branch is skipped (read it with `| default(...)`). Only `parallel` branches are isolated
(each gets a copy, merged back afterwards); `repeat`'s `repeat` var and a parallel-`protected` `wait`
are the sole truly-local names.

### The strategy matrix

The wizard offers **5 options**: notify-only plus 4 recovery strategies (one per
action shape). The Health Check is a separate per-recovery toggle
(`behavior.health_check`, default on), not a strategy variant:

```
notify    → noop
switch    → switch_cycle
action    → action_call
actions   → action_cycle
poe_port  → poe_port      (own staged verify; + device Health Check when enabled)
```

A strategy maps to a `driver type`; `behavior.health_check` (the toggle) decides
whether the engine's VERIFY step runs. Auto-PoE keeps its own staged verify (port
goes offline → comes online) on top of the device Health Check (when enabled).

**Auto-PoE remembers its port.** A device that is down can age out of the
switch's FDB/LLDP neighbour table, so resolving it live would find *nothing*
exactly when recovery is needed. The **fabric** keeps a last-known `id → port`
cache and learns continuously: it watches every configured port's id-entity
(`_rewatch` → `_on_change` → `_relearn`/`_learn`), so it caches the mapping the
moment the neighbour table reports the device — not only on a health event. At
recovery time a single live match still wins (and refreshes the cache); on
**zero** live matches `resolve_with_reason` falls back to that last-known port —
but only if that port currently reports *nothing* connected; if it now serves a
*different* live id (the device was re-cabled away) the stale entry is dropped and
it blocks ("no port matches") rather than cycling the wrong device (logged at
WARNING); an **ambiguous** (>1) match still blocks. The cache lives in
the fabric (not per guard) and is persisted in the Store under `_poe_cache`; the
`poe_port` driver is a thin adapter that just delegates resolve + cycle to it.

### PoE fabric & the `repair_poe_port` service

The `poe_port` driver cycles a port from *inside* a guard. Some recoveries instead
need to cycle a port from an **action** — e.g. cut PoE, wait for ping, then *reload*
a config entry (the lamps only return after the reload), a sequence a driver can't
express. The **PoE fabric** (`core/poe.py`) is that shared, port-level primitive: a
domain-singleton holding the live + last-known `id → port` map (same resolution as
`poe_port`, watching every port's id-entity), a per-port **status**
(`good` / `recovering` / `failed`) and a per-port **in-flight cycle**. It backs the
**`necromancer.repair_poe_port(id)`** service — blocking, and **coalesced** per port:
concurrent callers (multiple guards, automations) join the one in-flight cycle and
share its result instead of each cycling the port. Each status change is fired as a
`necromancer_poe_port` event. The
fabric is wired in `__init__` (singleton in `hass.data`, port list + cache from the
Store). The staged cycle lives **only** in the fabric — the `poe_port` driver delegates
resolve + cycle to it (`can_recover` → `resolve_with_reason`, `recover` → `repair`), so a
guard and the service share one cache and coalesce onto one in-flight cycle per port.

---

## 6. Guard linking (groups)

Guards that share a root cause are grouped so only **one** recovers and the rest
follow — e.g. a *ping* guard and a *lamps-unavailable* guard on the same Hue bridge.

- **Declaration & closure.** Every recover guard has a collapsed *Linked guards*
  multi-select (`linked_guards` = partner subentry_ids). The relation is
  **undirected + clique-closed**: `core/links.py` (`link_components`) builds connected
  components over the union of all declarations, so a one-sided link still reads and
  behaves as a full mutual group. The config flow reads the closure for the form
  default; `__init__` reads it to give each engine its effective partners. Only
  **recover** guards take part — both the flow's options *and* the setup closure
  exclude notify-only guards, so a guard reconfigured to notify-only drops out of
  every group (no inert ghost member). Unlinking clears the edge on **both** sides
  (`_apply_link_removals`), so the only way out of a group is to clear *all* its
  partners (a single shared partner re-forms the clique).
- **Coordination.** When a guard starts recovery the engine calls `self.links.notify_start()`
  (`LinkCoordinator` in `core/links.py`), which calls each partner's coordinator
  (`peer.links.on_partner_repair_start`) **and** fires a `necromancer_guard_repair` bus event
  for outside automations). A partner that isn't already busy enters a **follow hold**
  (RECOVERING, no own action) and suppresses its own health-driven transitions. When
  the leader finishes (`self.links.notify_done(success)` → each partner's
  `on_partner_repair_done`), each follower re-validates (`validate_after_repair`):
  - healthy → it settles through the **same `_recover_success` path** (cooldown +
    stats) as the leader, instead of snapping back to OK — but called with
    `via_link=True`, so its `recovery_success` **notification is suppressed by
    default** (one root-cause repair → one success push, the leader's). Opt in per
    guard with `behavior.notify_follower_success` (a toggle in the *Linked guards*
    section). The `necromancer_guard_repair` event still fires per guard regardless;
  - still unhealthy **and the leader succeeded** → only the follower's device is
    still down, so it falls back to its own recovery;
  - still unhealthy **and the leader failed** → the shared cause is unfixed, so the
    follower **escalates** (`linked_repair_failed`) rather than self-recovering and
    re-triggering the group (no cascade).
- **Arbitration is first-come.** A guard claims the leader role *synchronously* in
  `_start_cycle` (sets `RECOVERING` before the cycle task runs), so a linked partner
  whose debounce elapses in the same tick already sees it as repairing
  (`links.find_repairing_partner`) and follows —
  no double-cycle even on simultaneous trips.
- **Auto-off means off.** A guard whose `auto` switch is disabled never participates
  in a group repair: instead of following, if its own device is affected it
  **escalates** (`no_auto_recovery`). It is never silently fixed by a partner.

---

## 7. Notifications as actions

There are no fixed notify targets. Each guard optionally defines a **notify
action** (an `ActionSelector` sequence) that runs on events
(problem / recovery / escalation). Necromancer resolves a localized message and
exposes it to the action as Jinja variables:

| Variable | Value |
|---|---|
| `message` | The full ready-made line, `"<name>: <event_text>"`. |
| `name` | The guard name. |
| `event_text` | The localized event text **without** the name (so the user can compose their own line / avoid duplicating the name in a title). |
| `event` | The notify key (`recovery_success`, …). |
| `attempt` / `max` / `attempts` / `reason` | Event params, where applicable. `attempts` is the plural-correct phrase ("1 Versuch" / "3 Versuche"). |

The texts (`NOTIFY_MESSAGES` in `const.py`) are the **name-less** `event_text`;
`core/notify.py` (`_resolve`) prepends `"<name>: "` for `message` and computes `attempts`.
They're phrased for **TTS** — numbers as words ("1 von 2", not "1/2"), no
slashes/parentheses. So `message: "{{ message }}"` just works; the user decides
whether/how to notify. The action runs **detached** so a user delay never stalls
the engine.

---

## 8. Config flow (`config_flow.py` + `config_flow_helpers/`)

The handler classes stay in `config_flow.py` (which must remain a file — hassfest
requires it); the schema/selector builders live in the `config_flow_helpers`
package (`schemas.py` + reactive `selectors.py`).

Steps: **source type → device & state → strategy → recovery/notification**. The
strategy step lists **notify-only** (first) plus the four recovery strategies;
picking notify-only routes to a notification step instead of a recovery one. There
is no separate "mode" field — the notify-vs-recover choice *is* the strategy choice.

- **Flat device step.** The device & health step is **section-less**: its fields
  (`name`, `entity_id`, `attribute`, `on_value`, `off_value` for state-based — or
  `template` for template-based — plus the optional `device_id`) are all
  **top-level** (`_health_fields` returns a flat dict). So the flow submits a flat
  dict — e.g. `{"name": …, "entity_id": …, "on_value": [...], "off_value": [...]}`
  for state-based, `{"name": …, "template": "…"}` for template-based, with
  `device_id` top-level — not nested `state_check`/`template_check`/`assigned_device`
  sub-dicts.
- **Sections (recover steps).** The recovery/notify forms still group fields into
  `data_entry_flow.section`s with a heading and description (recovery action,
  behaviour, notification, linked guards, and — only when a device is assigned —
  *reload* the assigned device's integration after a repair; ports: switch /
  recognition / status / timing). Sections nest their values, so submitted input is
  flattened back up (`_flatten_sections`); on the now-flat device step the same
  helper is a harmless no-op.
- **Reactive selectors.** Attribute and state pickers follow their sibling entity
  field live via a per-field `context` mapping (`filter_entity` / `filter_attribute`).
  The reacting field and the entity it follows must sit in the **same form scope**:
  on the flat device step they share the top-level form; inside a port (which keeps
  its sections) they stay in the same section, since each section renders its own
  nested `ha-form` that regenerates context from the section’s data.
- **Own entities excluded — scoped.** The switch/actuator/port pickers exclude
  **all** Necromancer entities (`_own_entities`) — you never power-cycle a view
  entity. The **health** picker excludes only **this guard's** entities
  (`_own_guard_entities`, by `subentry_id` prefix), so a self-loop can't be picked
  but **other** guards' `*_status` / `*_health` stay selectable — that's what
  enables **supervisor / staged guards** (a template-health guard watching other
  guards). A genuine self-reference is still caught by the feedback-loop check.
- **Config validation timing.** `engine._check_config` (missing/disabled health,
  driver errors, blind-template, self-reference loop) is scheduled **by `__init__`
  after `async_forward_entry_setups`**, wrapped in `async_at_started`. So it runs
  once HA is started *and* the guards' own view-entities are registered — the
  self-reference check sees them even for a guard added at runtime (not just after
  the next restart).
- **Auto-recovery is not a setup field.** It is the per-guard runtime switch
  entity (Store-persisted); guards start with it on.
- **Options flow (ports).** A button menu (`async_show_menu`) over the flat port
  list: add / edit / delete a port (edit reuses the `add_port` step), plus
  **import / export** for bulk edits. *Export* multi-selects ports (all
  pre-selected) and dumps them to YAML; *import* parses pasted YAML and either
  **merges** (upsert by `label`) or **replaces** the list — every port is
  validated (`_parse_ports_yaml` → `_normalize_imported_port`: required
  label/actuator/status_entity, timings numeric and ≥ 0) and nothing is applied
  on error (the reason is surfaced via `description_placeholders`). YAML
  round-trips cleanly: `_ports_to_yaml` quotes on/off values and import coerces
  YAML 1.1 booleans (`on`/`off`/`yes`) back to strings, so the bool footgun can’t
  corrupt a status list.
- **Translations.** Custom components ship **no `strings.json`** (it is a Core
  build-time source); `translations/en.json` is the source HA loads directly, and
  `translations/de.json` mirrors it. HA renders config translations via **ICU
  MessageFormat**, so descriptions must contain **no `{…}` braces** except real
  `description_placeholders`.

---

## 9. Entities & platforms

Per recover guard, five pure-view entities (one device per guard, or attached to a
linked device): `sensor.*_status`, `binary_sensor.*_health`, `switch.*_auto_recovery`
(`entity_category: config`), `button.*_revive`, and `event.*_recovery` (event types
`recovered` / `escalated` / `blocked`, fired from `_recover_success` / `_escalate` via
the engine's `add_event_listener` hook). Notify-only guards omit the switch, button and
event. Linking to an existing device uses the Battery-Notes pattern (`device_info=None` +
`entity.device_entry`) so Necromancer never claims ownership of a foreign device.

The status sensor's attributes are intentionally lean — `attempt`, `recover_count`,
`last_recover`, `target`, `snooze_until`; auto-recovery is the switch (not duplicated as
an attribute) and "last seen healthy" is left to state history.

**Per-guard services** are registered on the sensor platform via
`async_register_entity_service` (targeted at `sensor.*_status`; device/area targets
expand to it): `reset`, `snooze(duration)`, `unsnooze`, `notify_guard` (see §3). The status sensor
also carries the `snooze_until` attribute. Recover-now and arm stay the button/switch
rather than duplicating them as services. The remaining services are **domain-level**
(no entity target): the port-level `repair_poe_port` (§5), the maintenance-mode
`snooze_all` / `unsnooze_all` (§3), and the two **response services** `check_health`
(returns a guard's current Health State) and `wait_for_health(timeout, check_first)`
(awaits OK / timeout) — both take a `guard` status entity and reuse that guard's own
Health Check, so a recovery script can poll Health out-of-band.

---

## 10. Module map

```
__init__.py        setup: build one DeviceEngine per device subentry, inject
                   ports into poe_port guards, resolve link groups, wire the PoE
                   fabric + repair_poe_port service, reconcile devices/entities, Store
core/engine.py          the state machine, timing, persistence, health wiring (delegates
                   linking to LinkCoordinator)
core/state.py           the GState enum (re-exported by core/engine.py)
config_flow.py     service + device-subentry + options(ports) flow handler classes
                   (must stay a file — hassfest)
config_flow_helpers/   schemas.py (all schema/section builders, _build_data, YAML
                   port import/export) + selectors.py (reactive Live* selectors)
const.py           keys, defaults, strategy/source constants
core/links.py           guard-link grouping (connected components / clique closure) +
                   LinkCoordinator: per-engine runtime link protocol (start/hold/verify)
core/poe.py             PoE fabric: shared id→port resolver, per-port status + coalesced
                   in-flight cycle, repair service
entity.py          base entity (DeviceInfo, unique_id, link handling)
sensor/binary_sensor/switch/button/event.py   the view entities (+ per-guard
                   services on the sensor platform: reset/snooze/unsnooze)
core/actions.py         validate + run user action sequences (Script helper)
core/notify.py          resolve localized message + run the notify action (detached)
core/health/            base, entity_state, template
core/drivers/           base, noop, switch_cycle, action_call, action_cycle, poe_port
core/policies/          base, standard, notify
```

---

## 11. Data flow (one recovery cycle, `switch` with Health Check on)

```
health entity changes
  → engine._evaluate() → HealthSource.evaluate() = UNHEALTHY
    → SUSPECT (debounce timer)
      → debounce elapsed, policy allows (auto on)
        → RECOVERING → driver.can_recover() ok → driver.recover() (off→delay→on)
          → [reload_entry? delay + reload assigned device's config entry]
          → VERIFY → _wait_health_ok(boot_window)
              ├─ health OK in time → recover_success → COOLDOWN → OK
              └─ timeout → attempt<max ? retry : ESCALATED
  (every transition persists to the Store; notify action runs per event)
```

> For every timer, the full per-state timing, the PoE/link timelines and an
> exhaustive case catalogue, see **[timing.md](timing.md)**.
