# Necromancer — Timing & case reference

A complete map of **every timer** in the engine and **every case** the system
handles, grouped by area. This is the companion to [architecture.md](architecture.md)
(structure) and [testing.md](testing.md) (what each case asserts). Defaults are the
shipped values from `const.py`; all of them are per-guard configurable in the wizard
(except the fixed internals noted as such).

---

## 1. Timing parameters

| Parameter | Default | Scope | Where it bites | Meaning |
|---|---|---|---|---|
| `debounce` | **120 s** | per guard | `_enter_suspect` → `async_call_later` | How long a guard must stay unhealthy (`SUSPECT`) before recovery is allowed to start. Absorbs blips. |
| `boot_window` | **180 s** | per guard (`*_check` only) | `VERIFY` → `_wait_health_ok` | How long to wait for health to read OK again after the recovery action before counting the attempt as failed. |
| `cooldown` | **600 s** | per guard | `_recover_success` → `async_call_later` | Settle pause after a successful recovery before returning to `OK`. While cooling down a fresh fault re-enters `SUSPECT` directly. |
| `max_attempts` | **2** | per guard (`*_check` only) | `_run_recovery_cycle` loop | How many recovery attempts before `ESCALATED`. Without a health-check there is exactly one attempt (fire-and-forget). |
| `off_on_delay` | **5 s** | per guard (switch / actions / poe) | the cycle | Pause between *off* and *on* in a power-cycle. |
| `reload_delay` | **10 s** | per guard (recover, only if a device is assigned + `reload_entry` on) | `_maybe_reload_device_entry`, after `recover()` and before VERIFY | Wait before reloading the assigned device's integration (config entry), so the just-repaired device has time to come up before HA reconnects. |
| `off_timeout` | **20 s** | per **port** | `poe_port` / fabric `_await_status` | Max wait for the port's status entity to read *offline* after cutting power (staged verify). |
| `on_timeout` | **60 s** | per **port** | `poe_port` / fabric `_await_status` | Max wait for the port's status entity to read *online* after restoring power. |
| `SAVE_DELAY` | **5 s** | global (fixed) | `Store.async_delay_save` | Debounce on writing runtime state to disk; a flush on unload guarantees no loss across a reload. |
| boot grace | — | global (fixed) | `async_at_started` | Referenced-entity/service existence is only checked **after** HA has fully started, so a not-yet-registered dependency during boot is not flagged. |
| auto-recovery | **on** | per guard (runtime) | `switch.<guard>_auto_recovery` | Not a setup timer but gates whether recovery (own *or* following a link) runs at all. |

`debounce` is the only timer for a **notify-only** guard; it has no recovery, verify,
cooldown or attempts.

---

## 2. The recovery clock (one full `*_check` cycle)

```
t0   health entity changes → _evaluate() → UNHEALTHY
       │  state OK → SUSPECT, start debounce timer
       ▼
t0+debounce   _debounce_done
       │  still UNHEALTHY?  no  → back to OK (blip absorbed)
       │  auto off?         yes → ESCALATED (no_auto_recovery)
       │  partner repairing? yes → follow (see §5)
       │  else → _start_cycle: claim RECOVERING *synchronously*, spawn cycle task
       ▼
       RECOVERING   attempt n/max
       │  driver.can_recover() → (False) → ESCALATED (recovery_blocked)
       │  driver.recover()      → raises  → failed attempt (retry/escalate)
       │  no health-check?      → success now (assume it worked)
       ▼
       VERIFY   _wait_health_ok(boot_window)
       │  health OK within boot_window → success
       │  timeout & attempt < max      → next attempt (back to RECOVERING)
       │  timeout & attempt == max     → ESCALATED (recovery_failed)
       ▼
t_ok   COOLDOWN   _recover_success: recover_count++, last_recover=now, start cooldown timer
       ▼
t_ok+cooldown   _cooldown_done
          unhealthy again → SUSPECT (straight back in)
          else            → OK
```

There is **no inter-attempt delay** beyond the action's own runtime: a failed VERIFY
loops straight into the next `RECOVERING`. The "pause after an attempt" the UI mentions
is the **cooldown** (post-success), not a retry gap.

---

## 3. State machine — per-state timing & exits

| State | Entered when | Timer running | Exits |
|---|---|---|---|
| `OK` | healthy, or after cooldown | none | → `SUSPECT` on UNHEALTHY |
| `SUSPECT` | OK→UNHEALTHY | **debounce** | → `OK` (recovered/blip) · → `ESCALATED` (auto off / policy) · → follow (partner) · → `RECOVERING` (start cycle) |
| `RECOVERING` | cycle starts (or claimed) | none (driver runs) | → `VERIFY` (with check) · → `COOLDOWN` (no check) · → `ESCALATED` (blocked) |
| `VERIFY` | after the action, `*_check` | **boot_window** | → `COOLDOWN` (healthy) · → `RECOVERING` (retry) · → `ESCALATED` (out of attempts) |
| `COOLDOWN` | success | **cooldown** | → `OK` (healthy) · → `SUSPECT` (unhealthy) |
| `ESCALATED` | gave up / blocked / auto-off | none | → `OK` automatically once health returns (clears the verdict) |
| `SNOOZED` | operator `necromancer.snooze` / `snooze_all` | **remaining snooze duration** | → `OK` (auto-resume on elapse, re-derives from health) · → `OK` (`unsnooze` / `unsnooze_all`, early). Health is ignored while snoozed. |

`ESCALATED` and `SNOOZED` are the **persisted transients** — a dead device gets no free
retry on reboot (`ESCALATED` self-clears via `ESCALATED → OK` when health comes back),
and a deliberate snooze survives a restart (re-arms the remaining time, or resumes
immediately if it already elapsed).

---

## 4. PoE cycle timing

Both the `poe_port` driver and the `repair_poe_port` fabric run the same staged cycle
against a port's actuator + status entity:

```
cut power (turn_off actuator)
  → await status == off,  up to off_timeout (20 s)   [timeout = WARNING, continue]
  → sleep off_on_delay (5 s)
  → restore power (turn_on actuator)
  → await status == on,   up to on_timeout (60 s)    [timeout → cycle reports not-online]
```

- `_await_status` returns **immediately** if the status already matches (so a port that
  is already off, or whose SNMP status lags, doesn't stall the cycle) and otherwise
  waits on a state-change listener up to the timeout.
- A `poe_port` **driver** then hands off to the engine's `VERIFY` (boot_window) against
  the *device* health — so there are two verifies: the port came back (staged) **and**
  the device came back (health).
- The **fabric** (`repair_poe_port`) only does the port cycle; the *caller's* action
  (e.g. wait-ping → reload → delay) and the engine VERIFY do the rest. Concurrent
  callers on one port **coalesce** onto a single in-flight cycle (they join it, they
  don't queue a second); status changes fire `necromancer_poe_port`
  (`good` / `recovering` / `failed`).

---

## 5. Guard-link timing

Leader + follower of a linked group. The **leader** runs the normal §2 clock and, in
addition, signals the group:

```
leader: _start_cycle → claim RECOVERING (sync) → task: links.notify_start
            (direct call to each partner + necromancer_guard_repair "start" event)
   ... leader runs its recovery + VERIFY ...
        → finally: links.notify_done(success)  ("done" event)

follower (auto on): links.on_partner_repair_start
            → hold: RECOVERING, _following=True, debounce timer cancelled
            → _evaluate suppressed while following (device drop is expected)
        on links.notify_done → links.validate_after_repair(leader_success):
            VERIFY (boot_window):
              healthy            → _recover_success (COOLDOWN + stats), same as leader
              unhealthy + leader OK   → own recovery (only our device is still down)
              unhealthy + leader FAIL → ESCALATED (linked_repair_failed), no cascade
```

**Claim window (arbitration).** A guard sets `RECOVERING` *synchronously* in
`_start_cycle`, before the cycle task runs. So if two linked guards' debounces elapse in
the same event-loop tick, the first to reach `_debounce_done` claims, and the second —
checking `links.find_repairing_partner` — sees it and follows. Without the synchronous claim
both would start (the state was still `SUSPECT` until the task ran): the bug fixed in
`a2d4a03`.

**Auto-off follower.** A follower whose `auto` switch is off never holds; if its device
is actually unhealthy it `ESCALATED`s (`no_auto_recovery`) instead. Off means off — it is
never silently fixed by a partner.

**Timing note for the Hue recipe.** Ping returns ~seconds after a PoE cycle, but a Hue
bridge's API (and thus the lamps leaving `unavailable`) can lag ~2–3 min. So the action
does *wait-ping → reload → delay*, and `boot_window` must be generous (≥ the lamp-return
lag) or the verify fails while the device is genuinely on its way back.

---

## 6. Case catalogue

Every distinct case the system handles. *(C = confirmed by a test/probe; L = seen live.)*

### Health

| Case | Behaviour |
|---|---|
| entity value ∈ `on_value` | `OK`. |
| entity value ∈ `off_value` | `UNHEALTHY`. |
| `unavailable`/`unknown`, not listed in off | `UNKNOWN` → never triggers (no false alarm). C |
| `unavailable` listed in `off_value` | treated as the fault that triggers recovery. |
| template renders truthy / falsy | `OK` / `UNHEALTHY`. |
| template render error / empty / `none` / `unknown` | `UNKNOWN` (no false alarm). C |
| **disabled entity** in a template | `states()` = `unknown`, **not** `unavailable` — a template checking `unavailable` will *not* fire. (Sharp edge; use a real outage or a state override to simulate.) C |
| health entity renamed | the registry listener re-points; guard keeps watching. |
| health entity removed / disabled | logged at ERROR ("guard is blind"); re-enabled → INFO. |
| referenced entity missing **at boot** | not flagged until after HA start (`async_at_started`). |

### Recovery

| Case | Behaviour |
|---|---|
| unhealthy < debounce then healthy | blip absorbed, no recovery. |
| `can_recover()` false (missing switch / no port / invalid action) | `ESCALATED` (`recovery_blocked`), no blind action. C |
| `recover()` raises (e.g. missing service) | failed attempt → retry/escalate, never false success. C |
| no health-check (fire-and-forget) | one attempt, assumed success; continuous monitoring re-triggers if it didn't work. |
| health returns within boot_window | success → cooldown. L |
| boot_window times out, attempts left | next attempt. |
| boot_window times out, no attempts left | `ESCALATED` (`recovery_failed`). |
| auto-recovery off + unhealthy | `ESCALATED` (`no_auto_recovery`), no action. C |
| notify-only guard | raises `problem_detected`, never recovers. |
| manual recover button | bypasses debounce + auto gate, runs a cycle now. |
| unhealthy again during cooldown | straight back to `SUSPECT`. |

### Persistence (across restart)

| Case | Behaviour |
|---|---|
| `ESCALATED` + still unhealthy | restored as `ESCALATED` (no free retry). |
| `ESCALATED` + healthy again | auto-clears to `OK`. |
| `recover_count`, `last_recover`, `last_seen`, `auto` | restored from the Store. |
| transient `RECOVERING`/`VERIFY`/`COOLDOWN` | **not** restored — re-derived from live health. |
| PoE last-known port (fabric `_poe_cache`) | restored, so a `poe_port` guard keeps its fallback target across a reboot. |
| `SNOOZED` + snooze still active | restored as `SNOOZED`; the remaining snooze time is re-armed on start. |
| `SNOOZED` + snooze already elapsed | resumes to `OK` and re-derives from live health. |

### Snooze (operator `necromancer.snooze` / `unsnooze`)

| Case | Behaviour |
|---|---|
| `snooze` a guard | `SNOOZED` for the duration; health ignored (no detection/recovery). |
| snooze timer elapses | auto-resumes to `OK`, re-deriving from live health. |
| `unsnooze` / `unsnooze_all` | lifts the snooze early, back to `OK` + re-derive. |
| `snooze` during an active recovery (`RECOVERING`/`VERIFY`) | refused — raises `ServiceValidationError` (`snooze_during_recovery`). |
| `snooze_all` with some guards recovering | snoozes the rest; busy guards skipped (WARNING), no error raised. |
| snooze persisted across restart | restored as `SNOOZED`; remaining time re-armed (or resumes if elapsed). |

### PoE (`poe_port` driver & fabric)

| Case | Behaviour |
|---|---|
| exactly one live id match | wins, refreshes the cache. |
| zero live matches, cached port known | falls back to last-known (WARNING). C |
| zero live, no cache | blocked ("no port matches"). |
| ambiguous (>1 live match) | blocked / `resolve` returns None (ERROR). C |
| device aged out of the switch table while down | learned-while-healthy cache lets recovery still fire. |
| port status sensor lags / already off | `_await_status` returns immediately or times out (WARNING) but the cycle continues. |
| concurrent `repair_poe_port` on one port | coalesced — callers join the in-flight cycle; one cycle, not N. C |
| `repair_poe_port` with unresolvable id | logs ERROR, returns False; the calling action continues (health-check still gates success). |

### Guard linking

| Case | Behaviour |
|---|---|
| link A→B (one-sided) | reads + behaves as a mutual group (closure). C |
| A–B, B–C | transitive: `{A,B,C}` one group; next edit shows all. C |
| stale partner id (partner deleted) | dropped from the effective group at runtime; self-cleans on next reconfigure. C |
| notify-only guard in the mix | excluded from the closure and from the flow options. C |
| one guard trips | it leads; linked partners follow (hold + re-verify), no parallel recovery. L |
| both trip simultaneously | first to `_debounce_done` claims `RECOVERING`; the other follows (no double-cycle). C |
| follower healthy after the shared repair | settles through `_recover_success` (cooldown + stats) like the leader. L |
| follower still unhealthy, **leader succeeded** | follower runs its own recovery (only its device is down). |
| follower still unhealthy, **leader failed** | follower `ESCALATED`s (`linked_repair_failed`); no cascade. C |
| follower with auto off | does not follow; escalates if affected (`no_auto_recovery`). C |
| unlink one side | the edge is cleared on both ends (`_apply_link_removals`). |
| uncheck one partner in a clique of 3 | re-forms via the shared partner — must clear *all* to leave. |
| `necromancer_guard_repair` event | fired on every leader start/done for outside automations. |

### Config flow

| Case | Behaviour |
|---|---|
| broken template at submit | rejected (invalid). |
| missing service in an action | escalates at run time, not a false success. |
| bad notify action | logged, no crash (notify runs detached). |
| port YAML import invalid | `import_failed` with the reason; list untouched. |
| import merge vs replace | upsert by `label` / overwrite. |
| reactive attribute/state pickers | follow the sibling entity within the same section. |
| own entities in pickers | excluded (no self-watch / self-switch). |

---

## 7. Timing footguns (known sharp edges)

- **Ping ≠ API-ready.** A device answering ping is not the same as its integration being
  usable again (Hue lamps lag minutes). Verify against the *real* signal and size
  `boot_window` to the slowest return.
- **SNMP / neighbour-table lag.** Port `*_aktiv` and `*_nachbarn` sensors update on a
  poll, so they can lag a real power state by seconds; the cycle tolerates this
  (immediate-match + timeout-continue), but don't set `off_timeout`/`on_timeout` below
  the poll interval.
- **Disabling an entity ≠ making it `unavailable`.** Registry-disable yields `unknown`;
  a template watching `unavailable` won't trigger. Simulate outages by cutting power or
  overriding state, not by disabling.
- **Notify action runs detached.** A user `delay` in the notify action never stalls the
  engine — but it also means a notify is not awaited; failures only show in the log.
- **Cooldown is post-success, not a retry gap.** Retries are back-to-back (only the
  action's own runtime separates them). Use `boot_window` to give a device time to come
  back *within* an attempt, not `cooldown`.
- **MCP is down right after a Core restart.** Operational, not engine: drive the live
  API via the Supervisor proxy (`http://supervisor/core/...` + `$SUPERVISOR_TOKEN`)
  until the MCP add-ons reconnect.
