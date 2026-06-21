# Necromancer — Test concept

Three levels, smallest-first. The manual regression checklist proves end-to-end
behaviour today; pure logic should move into `pytest` over time.

> The exhaustive list of cases each test guards — with their timing — lives in
> **[timing.md](timing.md)** (§6 case catalogue). This file says *how* to test them.

---

## 1. Invariants to protect

These are the properties a change must never break — every test exists to guard
one of them:

1. **No false alarm.** Ambiguous health (missing entity, render error, empty,
   `unknown`/`unavailable`) → `UNKNOWN`, never `UNHEALTHY`. No recovery is
   triggered on `UNKNOWN`.
2. **No false success.** A recovery only counts as success if the action ran
   without raising *and* (for `*_check`) health verified OK. A raising
   `recover()` is a failed attempt → retry/escalate.
3. **Verify is possible.** Every `HealthSource.evaluate()` is callable on demand,
   so the VERIFY step works (this is why health is a *template*, never a momentary
   trigger).
4. **State survives restart.** `ESCALATED`, `recover_count`, and the `auto` flag
   are restored from the `Store`; transient states are not.
5. **Pre-flight blocks blind actions.** `can_recover()` returns `(False, reason)`
   when a referenced thing is missing → `recovery_blocked`, no blind cycling.
6. **UI correctness.** Reactive selectors resolve within their section; config
   translations carry no ICU `{…}` braces; every step has a description; own
   entities are excluded from pickers.
7. **Linked guards coordinate, never compete.** When one guard in a group repairs,
   the others *follow* (hold + re-verify afterwards) and never launch a parallel
   recovery for the same root cause. The link relation stays symmetric and
   clique-closed; the only way out of a group is to clear all of its partners.
8. **Shared port recovery is coalesced.** `repair_poe_port` cycles a port via a
   per-port in-flight task, so concurrent callers join one cycle and share its
   result instead of double-cycling.
9. **Operator snooze suspends cleanly.** A snoozed guard ignores health entirely
   (no transitions, no alerts), survives a restart (re-arming the *remaining* time),
   auto-resumes on elapse, and is refused (`ServiceValidationError`) mid-recovery —
   distinct from auto-off, which still detects and escalates.

---

## 2. Level 1 — unit (pure logic / real `hass`)

Fast, deterministic. Three runnable in-process modules cover this level today (run
them with the dev venv, see §5): **`tests/test_units.py`** (21), **`tests/test_poe.py`**
(16), **`tests/test_engine.py`** (34). On top sits a **pytest suite on HA's native test
harness** (`tests/suite/`, run via `pytest tests/components/necromancer/`) that automates
Level 2 in-process — see §3. Each row maps to an invariant:

| Module | What to assert | Covered by |
|---|---|---|
| `core/health/entity_state.py` | value-list mapping → OK/UNHEALTHY/UNKNOWN; unavailable/unknown → UNKNOWN; explicit-off-wins; legacy `healthy_state` fallback. | `test_units` |
| `core/health/template.py` | `result_as_boolean` cases (`true/false/on/0/'on'`); empty/`none`/`unknown` → UNKNOWN; render error → UNKNOWN. | `test_units` |
| `core/drivers/*.can_recover` | missing switch / missing+invalid action → `(False, reason)`; the poe_port adapter blocks on no/ambiguous match. | `test_units`, `test_poe` |
| `config_flow` helpers | `_flatten_sections` (nested→flat), `_as_list`, `_build_data` (health block per source type, behaviour per check), `_current_strategy`, `_source_type_of`. | `test_units` |
| `config_flow` ports YAML | `_parse_ports_yaml`/`_normalize_imported_port`: required `label`/`actuator`/`status_entity`; reject not-a-list / empty / `null` / list-of-scalar / malformed YAML / non-numeric **or negative** timing; trim, scalar→list, missing/empty status→default, int→str. `_ports_to_yaml` round-trips; import **merge** = upsert by `label`, **replace** = overwrite. | `test_units` |
| `core/actions.py` | `async_validate` normalises `service`→`action`; invalid sequence raises `vol.Invalid`. | `test_units` |
| `core/links.py` | `link_components` / `group_of`: undirected union → connected components (clique closure); transitive (A–B, B–C ⇒ `{A,B,C}`); a one-sided link reads symmetric; stale ids dropped. | `test_units` |
| `core/poe.py` (fabric) + `poe_port` driver | `resolve_with_reason`: one live match wins (refreshes cache) → last-known cache → ambiguous/none with a reason; `repair` sets status `recovering`→`good`/`failed`, with concurrent callers **coalescing** onto one in-flight cycle (`test_concurrent_callers_coalesce`); a status change fires `necromancer_poe_port`; the `poe_port` driver delegates resolve+cycle to the fabric (one cache, one cycle, **shared with the service** — `test_driver_and_service_coalesce`). | `test_poe` |

## 3. Level 2 — integration (HA test harness or dev container)

Most of this is now **automated in `tests/suite/`** — the HA-harness pytest suite
(`MockConfigEntry` + `subentries_data` → real `async_setup_entry`, platforms, registries):
config/subentry/options flow (every step + reject path), the view entities, the operator
services, and per-guard setup isolation. The dev-container REST/WS harness (§5) remains for
exploratory and true end-to-end live checks. Drive the real flows and the engine:

- **Config flow → engine setup**: each strategy + source type builds a valid
  guard; sections flatten; `poe_port` injects the flat port list.
- **State machine** *(automated: `tests/test_engine.py`, real hass + time-travel)*:
  happy path (recover→verify→cooldown→ok, `recover_count++`); debounce blip
  absorbed; max-attempts → `ESCALATED`; raising driver = failed attempt; auto-off →
  `ESCALATED`; manual recover (and ignored while a cycle is already running); cooldown→suspect.
- **Persistence across restart** *(automated: `tests/test_engine.py`)*: ESCALATED
  stays (still unhealthy) / auto-clears (healthy again); `recover_count` and `auto`
  survive; snapshot round-trip.
- **Health robustness**: rename-following; disable/enable live; remove; startup
  already-unhealthy detection.
- **Corner cases**: broken template rejected at submit; missing service in an
  action → escalate (not false success); bad notify action → logged, no crash.
- **Port import/export (options flow)**: the menu exposes import + export; import
  **merge** (upsert by `label`) vs **replace**; an invalid paste returns
  `import_failed` with the reason in `description_placeholders` and leaves the list
  untouched; `import_mode` omitted defaults to merge; export multi-select (all
  pre-selected, empty selection tolerated) → round-trip YAML. Driven
  **non-destructively** (never call `save`), so the entry's real ports are safe.
- **Auto-PoE fallback**: build a poe_port guard whose device id is a settable
  entity state; let it learn the port while healthy, then make the device vanish
  (id gone) **and** go unhealthy → the recovery must cycle the *cached* port
  (WARNING "falling back to last-known port"), not block with "no port matches".
- **Guard linking**: two linked guards on a shared health source; trip one → the
  other logs "linked guard is repairing — following", holds (no own action), and
  re-validates after → both end OK and the follower also enters cooldown. Closure:
  a one-sided link still shows the partner on *both* guards' next edit; unlinking
  from one side clears the edge on both; a notify-only guard never appears in a
  group. Corner cases (all covered by a standalone engine probe):
  - **Arbitration**: simultaneous debounce → the leader claims `RECOVERING`
    synchronously in `_start_cycle`, so the other follows (no double-cycle).
  - **Auto-off**: a follower with its `auto` switch off does **not** follow — if its
    device is affected it escalates (`no_auto_recovery`). Off means off.
  - **Leader failed**: leader escalates and the follower is still unhealthy → the
    follower escalates (`linked_repair_failed`), it does **not** self-recover and
    re-trigger the group (no cascade).
- **`repair_poe_port` service**: a call resolves the id and cycles the port
  (status `recovering`→`good`); a second concurrent call **joins the in-flight cycle**
  instead of double-cycling. Verified live end-to-end via a real PoE-bridge outage
  (pull power → linked ping+lamps guards → one cycles the port → both verify OK).
- **View entities** *(`tests/suite/test_{sensor,binary_sensor,switch,button,event}.py`)*:
  status (enum + lean attributes), health (connectivity; `UNKNOWN` → `unavailable`),
  auto-recovery switch (`entity_category: config`, writes through to `auto`), revive
  button (manual cycle, bypasses debounce), and the recovery `event` (fires
  `recovered` / `escalated` / `blocked`; absent on notify-only guards).
- **Operator services** *(`tests/suite/test_services.py`)*: `reset` (clears `ESCALATED`,
  re-derives); `snooze`/`unsnooze` (→ `SNOOZED`, ignores health, auto-resumes, refused
  mid-recovery); `snooze_all`/`unsnooze_all` (bulk, no target, busy guards skipped
  best-effort).

These run today against the dev container by driving the REST/WS flow API and
asserting on `sensor.*_status` + the error log (see the regression checklist for
exact steps).

## 4. Level 3 — agent regression checklist

[`AGENT_REGRESSION.md`](AGENT_REGRESSION.md) is the agent-runnable, priority-ordered
checklist (P0/P1/P2): each item is a tickable *Prüft / Files / Treiber / Assert /
Cleanup* block an agent drives via file inspection and the live-test helper API. Run
the **P0** block after any engine, persistence, health, or config-flow change. Each
item names the expected log line / entity state so a run is unambiguous.

---

## 5. Dev-container live harness

The integration is developed inside an HA-core dev container with the package
mounted live. Two complementary harnesses are used:

**Standalone logic (no HA, no token).** Run a script with the dev venv python and
`PYTHONPATH=<config dir>` so `from custom_components.necromancer import config_flow`
resolves. For flow methods, instantiate the flow and monkeypatch `async_show_form`
/ `async_show_menu` / `async_step_init` to capture their result, then call the real
`async_step_*` and assert on the mutated state. This is how the port import/export
logic is covered (parse / validate / merge / replace / export round-trip), fast and
without a running instance.

**Live flow (running HA + token).** Pattern used throughout development:

- Drive subentry/options flows via `POST /api/config/config_entries/{subentries,options}/flow`
  — open with `{"handler": …}`, navigate a menu with `{"next_step_id": …}`, submit a
  step by posting its fields. For read-only checks (import/export) **don't call
  `save`** and `DELETE` the flow at the end, so the real config stays untouched.
- Toggle test helpers (`input_boolean.test_*`, `input_select.test_state`, …) and
  read back `GET /api/states/...` + `/api/error_log`.
- Fetch served translations via WS `frontend/get_translations` to confirm keys
  render (and that selectors actually surface labels).

**Restart** — code changes need a module re-import. The process runs as
`python -m homeassistant -c ./config`; stop it with `pkill -f "[h]omeassistant -c"`
(the `[h]` keeps pkill from matching its own command line), relaunch in the
background, then poll `GET /api/config` until `state == "RUNNING"`.

A clean run starts from an empty service entry (delete leftover `device` subentries via WS
`config_entries/subentries/delete` and clear options ports).

---

## 6. Pre-commit gates

- `ruff check` / `ruff format` (custom-component exceptions: `D10x` docstrings and
  `TID25x` absolute-import/`__future__` rules don’t apply).
- `python -m py_compile` over all modules.
- Translation symmetry: `strings.json` keys == `en.json` == `de.json`; placeholder
  sets per key consistent; **no `{{`** in any description; every step has a
  `description`.
