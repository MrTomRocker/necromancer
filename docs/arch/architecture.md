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

## 2. Configuration model

Necromancer is a **single service** config entry (`integration_type: service`, added
once, blank). Everything else hangs off it:

| Thing | Where it lives |
|---|---|
| **Service** | One config entry (`data` blank). |
| **Guarded device** | A config **subentry** of type `device` (one per watched device). Added via *“Add device”*, edited via its *Reconfigure* button. |
| **PoE ports** | A flat list in the service entry’s **options** (`entry.options["ports"]`), managed via the **options flow**. |

One `DeviceEngine` is built per `device` subentry and lives in
`entry.runtime_data` keyed by `subentry_id`. Adding/changing a subentry reloads
the service; an options (ports) change also reloads it, so `poe_port` guards always
see a fresh port list.

There is **no per-area grouping** and no second config entry — an earlier
two-entry split was reverted because the HA frontend can’t filter the subentry
picker by type (it would offer both services for every “Add” button).

---

## 3. The engine state machine

`engine.py` runs a fixed state machine per guard. States
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

Key timing fields (the **behaviour** block):

| Field | Meaning |
|---|---|
| `debounce` | How long unhealthy before acting (filters blips). |
| `boot_window` | How long to wait for the device to come back (the VERIFY step). |
| `cooldown` | Pause after a recovery cycle before re-arming. |
| `max_attempts` | Retries within one cycle before escalating. |

**Health-check vs fire-and-forget.** A strategy can run *with* or *without* a
health-check:

- **With** (`*_check`): after `recover()`, the engine waits (event-driven, up to
  `boot_window`) for the HealthSource to read `OK`. Not OK within the window →
  retry up to `max_attempts` → `ESCALATED`.
- **Without**: `recover()` is assumed to have worked → straight to success; the
  continuous health monitoring re-triggers later if it didn’t. *(If `recover()`
  raises — e.g. a missing service — it counts as a failed attempt, never a
  success.)*

**Persistence.** Runtime state is persisted in a `Store`
(`.storage/necromancer.<entry_id>`), independent of the display entities:
`{state, attempt, recover_count, last_recover, last_seen, auto}`. On restart the
stats and `auto` flag are restored, `ESCALATED` is restored (then re-derived from
live health), and transient states (RECOVERING/COOLDOWN/VERIFY) are *not*
restored — they come back as OK/live-health. The display entities
(`sensor.*_status`, `binary_sensor.*_health`, `switch.*_auto_recovery`,
`button.*_recover`) are **pure view**; the `Store` is the source of truth.

---

## 4. Health sources (`health/`)

`evaluate() -> Health` is always callable (so the VERIFY step can re-check). A
source that tracks something other than entity states registers its own listener
in `async_setup(on_change)` (returns an unsub) and exposes an empty
`watched_entities`.

| Type | What it is | Healthy when |
|---|---|---|
| `entity_state` | One entity’s state or attribute vs on/off **value lists**. | value ∈ `on_value`; ∈ `off_value` → unhealthy; else `UNKNOWN`. Unavailable/unknown → `UNKNOWN`. |
| `template` | An inline Jinja template that returns `true`/`false`. | `result_as_boolean(render)` → OK/unhealthy; render error, empty, `none`, `unknown`/`unavailable` → `UNKNOWN`. |

The **template** source is the inline alternative to building a template *entity*.
Because a template is a continuous, checkable expression (unlike a momentary
*trigger*), it supports the full recover→verify cycle. It is tracked via
`async_track_template_result`, so health re-evaluates whenever a referenced
entity changes.

The wizard’s first step picks the source type (`state_based` / `template_based`).

---

## 5. Recovery drivers (`drivers/`)

`recover()` performs the repair; `can_recover()` is a pre-flight guard that blocks
(→ `recovery_blocked`, no blind action) when something is missing.

| Driver | Action | Pre-flight (`can_recover`) |
|---|---|---|
| `switch_cycle` | `turn_off` → `off_on_delay` → `turn_on`. | switch entity exists. |
| `action_call` | Run one user-defined action sequence (script syntax). | action valid & non-empty. |
| `action_cycle` | Run an *off* action → delay → *on* action. | both actions valid. |
| `poe_port` | Resolve the device to a PoE port by `expected_id`, then cycle its actuator with staged status verify. | exactly one matching port. |
| `noop` | Nothing (used by notify-only guards). | — |

User actions are validated (`cv.SCRIPT_SCHEMA` + `async_validate_actions_config`)
and run via the `Script` helper (`actions.py`), blocking for recovery, detached
for notifications.

### The strategy matrix

The wizard offers **7 strategies** = 3 action shapes × {plain, +health-check} +
PoE:

```
switch          switch_check          → switch_cycle   (no verify / verify)
action          action_check          → action_call
actions         actions_check         → action_cycle
poe_port                              → poe_port        (own staged verify + health)
```

A strategy maps to a `driver type` + a `health_check` behaviour flag. Auto-PoE
keeps its own staged verify (port goes offline → comes online) on top of the
device health-check.

---

## 6. Notifications as actions

There are no fixed notify targets. Each guard optionally defines a **notify
action** (an `ActionSelector` sequence) that runs on events
(problem / recovery / escalation). Necromancer resolves a localized message and
exposes it to the action as Jinja variables:

| Variable | Value |
|---|---|
| `message` | The ready-made localized text (`recovery_attempt`, `recovery_success`, …). |
| `name` | The guard name. |
| `event` | The notify key. |
| `attempt` / `max` / `reason` | Event params, where applicable. |

So `notify.mobile_app_x` with `message: "{{ message }}"` just works; the user
decides whether/how to notify. The action runs **detached** so a user delay never
stalls the engine.

---

## 7. Config flow (`config_flow.py`)

Steps for a recover guard: **source type → device & state → strategy → recovery**
(notify-only guards stop after a notification step).

- **Sections.** Fields are grouped into `data_entry_flow.section`s with a heading
  and description (state check, behaviour, notification, assigned device; ports:
  switch / recognition / status / timing). Sections nest their values, so submitted
  input is flattened back up (`_flatten_sections`).
- **Reactive selectors.** Attribute and state pickers follow their sibling entity
  field live via a per-field `context` mapping (`filter_entity` / `filter_attribute`).
  The reacting field and the entity it follows must sit in the **same section**
  (a section renders its own nested `ha-form` that regenerates context from the
  section’s data).
- **Own entities excluded.** Entity pickers exclude Necromancer’s own entities
  (`exclude_entities`) so a guard can’t watch or switch its own status entities.
- **Auto-recovery is not a setup field.** It is the per-guard runtime switch
  entity (Store-persisted); guards start with it on.
- **Translations.** `strings.json` is the source; `translations/en.json` is an
  exact copy; `translations/de.json` mirrors it. HA renders config translations
  via **ICU MessageFormat**, so descriptions must contain **no `{…}` braces**
  except real `description_placeholders`.

---

## 8. Entities & platforms

Per guard, four pure-view entities (one device per guard, or attached to a linked
device): `sensor.*_status`, `binary_sensor.*_health`, `switch.*_auto_recovery`,
`button.*_recover`. Notify-only guards omit the switch and button. Linking to an
existing device uses the Battery-Notes pattern (`device_info=None` +
`entity.device_entry`) so Necromancer never claims ownership of a foreign device.

---

## 9. Module map

```
__init__.py        setup: build one DeviceEngine per device subentry, inject
                   ports into poe_port guards, reconcile devices/entities, Store
engine.py          the state machine, timing, persistence, health wiring
config_flow.py     service + device-subentry + options(ports) flows, schemas, sections
const.py           keys, defaults, strategy/source constants
entity.py          base entity (DeviceInfo, unique_id, link handling)
sensor/binary_sensor/switch/button.py   the four view entities
actions.py         validate + run user action sequences (Script helper)
notify.py          resolve localized message + run the notify action (detached)
health/            base, entity_state, template
drivers/           base, noop, switch_cycle, action_call, action_cycle, poe_port
policies/          base, standard, notify
```

---

## 10. Data flow (one recovery cycle, `switch_check`)

```
health entity changes
  → engine._evaluate() → HealthSource.evaluate() = UNHEALTHY
    → SUSPECT (debounce timer)
      → debounce elapsed, policy allows (auto on)
        → RECOVERING → driver.can_recover() ok → driver.recover() (off→delay→on)
          → VERIFY → _wait_health_ok(boot_window)
              ├─ health OK in time → recover_success → COOLDOWN → OK
              └─ timeout → attempt<max ? retry : ESCALATED
  (every transition persists to the Store; notify action runs per event)
```
