# Necromancer — Test concept

Three levels, smallest-first. The manual regression checklist proves end-to-end
behaviour today; pure logic should move into `pytest` over time.

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

---

## 2. Level 1 — unit (pure logic, `pytest`, no HA running)

Fast, deterministic, the first thing to add. The port-YAML row already has a
working standalone harness (§5); the rest are candidates. Each maps to an
invariant:

| Module | What to assert |
|---|---|
| `health/entity_state.py` | value-list mapping → OK/UNHEALTHY/UNKNOWN; unavailable/unknown → UNKNOWN; legacy `healthy_state` fallback; multiselect membership. |
| `health/template.py` | `result_as_boolean` cases (`true/false/on/off/1/0/'banana'/42`); empty/`none`/`unknown` → UNKNOWN; render error → UNKNOWN. |
| `drivers/*.can_recover` | missing switch / missing+ambiguous port / invalid+empty action → `(False, reason)`. |
| `config_flow` helpers | `_flatten_sections` (nested→flat), `_as_list`, `_watch_config`/`_watch_defaults`, `_build_data` (health block per source type, behaviour per check), `_current_strategy`, `_source_type_of`. |
| `config_flow` ports YAML | `_parse_ports_yaml`/`_normalize_imported_port`: required `label`/`actuator`/`status_entity`; reject not-a-list / empty / `null` / list-of-scalar / malformed YAML / non-numeric **or negative** timing; trim, scalar→list, missing/empty status→default, int→str, unknown keys dropped, unicode. `_ports_to_yaml` round-trips; bool/number-like values survive both ways (export quotes, import coerces YAML-1.1 `on/off/yes/no` back to strings — purely-numeric colon ids like `1:2:3` are the one thing to quote). Import **merge** = upsert by `label`, **replace** = overwrite; invalid import leaves the list untouched. *(A standalone harness — see §5 — exercises 35 of these against the real module.)* |
| `actions.py` | `async_validate` normalises `service`→`action`; invalid sequence raises `vol.Invalid`. |

## 3. Level 2 — integration (HA test harness or dev container)

Drive the real flows and the engine:

- **Config flow → engine setup**: each strategy + source type builds a valid
  guard; sections flatten; `poe_port` injects the flat port list.
- **State machine**: happy path (recover→verify→cooldown→ok, `recover_count++`);
  max-attempts → `ESCALATED`; auto-off → `ESCALATED`; manual recover; cooldown→
  suspect.
- **Persistence across restart**: ESCALATED stays (still unhealthy) / auto-clears
  (healthy again); `recover_count` and `auto` survive.
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

These run today against the dev container by driving the REST/WS flow API and
asserting on `sensor.*_status` + the error log (see the regression checklist for
exact steps).

## 4. Level 3 — manual regression checklist

`REGRESSION.md` (kept in the dev-docs area, not shipped) is the human-run,
priority-ordered checklist (P0/P1/P2). Run the **P0** block after any engine,
persistence, health, or config-flow change. Each item names the expected log
line / entity state so a run is unambiguous.

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
