# Project state

Durable hand-off so a fresh agent (e.g. Claude Code moved into the dev container)
is immediately in the picture. Update this when something material changes.

_Last updated: 2026-06-19._

## Where the code stands (branch `dev`)
- **PoE fabric is the single PoE authority** (`poe.py`): `resolve_with_reason`
  (live match → last-known cache → ambiguous/none, with reason + DEBUG trace +
  re-cabling warning), staged power-cycle, per-port `asyncio.Lock`, per-port status
  fired as `necromancer_poe_port`, service `necromancer.repair_poe_port(id)`. The
  `poe_port` driver is a thin adapter delegating to it — so a guard and the service
  share one lock + one cache (no concurrent double-cycle).
- **Guard linking** (`links.py`, engine): recover guards group via a collapsed
  multi-select; connected-component / clique closure; one repairs, the rest follow
  (hold → re-verify), with arbitration (synchronous `RECOVERING` claim), auto-off →
  escalate (no silent follow), leader-failure → followers escalate (no cascade),
  follower-success → cooldown like the leader. Fired as `necromancer_guard_repair`.
- **Pitfall fixes applied** (F1–F6): duplicate guard names rejected at submit;
  template self-reference feedback-loop warning + `referenced_entities()`; English
  policy reason constants (`REASON_AUTO_OFF`/`REASON_OBSERVE`); empty action/actions
  rejected at submit; README FAQ for link-visibility & health-check choice.

## Tests
Real-HA-core suites (run with `PYTHONPATH=<ha-core>:<ha-core>/config python …`):
`tests/test_units.py` (18), `tests/test_poe.py` (14), `tests/test_engine.py` (10),
`tests/test_integration.py` (8) — **50 total, all green**. Gates (ruff/format/
compile/translations/hassfest) green. Manual/live §3 items run against the dev HA
all passed; no new product pitfalls (the two harness "fails" were test artifacts:
a sub-second back-to-back create race that can't happen via the serial UI, and a
too-strict assert).

## Deploy status
- **dev branch**: pushed to `origin` through the H1b + test work.
- **Live (production HAOS 192.168.1.8)**: running fabric + linking + the two linked
  Hue-EG guards (Ping + Lamps, verified at a real bridge outage). **PENDING:** the
  F1–F6 pitfall fixes (commits `3aad1a4`, `24eae20`) are committed on `dev` but
  **not yet deployed to live** — deploy `const.py`, `policies/*`, `engine.py`,
  `health/*`, `config_flow.py`, `strings.json`, `translations/{en,de}.json`.

## Open / not done (deliberately)
- **M1**: extract the engine's link coordination into a `LinkCoordinator` (engine is
  ~600 LOC). Optional.
- **M2**: split `config_flow.py` (~1.4k LOC) into a package (`flows`/`schemas`/`ports`).
- **services.yaml descriptor** exists for `repair_poe_port`; keep in sync if the
  service grows.

## The Hue-EG recovery (live)
Two linked guards on the Philips hue-EG bridge (MAC `ec:b5:fa:12:fd:07` → PoE
`poe_switch_klein` Port 4; Hue config entry `01K6BNKXEM11FBWDATQ0SE4M2C`):
- "Hue-EG Ping": health `{{ is_state('binary_sensor.192_168_1_12','on') }}`.
- "Hue-EG Lampen": health = not all 5 reference lamps `unavailable`.
- Recovery (both, `action_check`, linked): `repair_poe_port(id)` → wait ping on →
  `reload_config_entry(hue)` → delay → verify. Notify via `script.necromancer_notify_eg`.
Replaced the old YAML automation `hue_bridge_eg_problem` + `script.poe_port_neustart`
(deleted). See [hue recipe in README](../../README.md#linked-guards-groups).
