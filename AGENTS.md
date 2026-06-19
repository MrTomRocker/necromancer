# AGENTS.md — working on Necromancer

Context for any AI agent (Claude Code, etc.) developing this integration. The
durable project memory lives here in the repo (versioned, travels with the mount)
— not in a per-machine auto-memory store.

Language: replies in **German**. Commits: **no `Co-Authored-By` trailer**. Never
read/print `secrets.yaml`.

## What this is
A generic self-healing framework for Home Assistant: a *guard* pairs one health
signal with one recovery and runs a fixed lifecycle (detect → confirm → recover →
verify → settle). User docs in [README.md](README.md); design in
[docs/arch/architecture.md](docs/arch/architecture.md); every timer & case in
[docs/arch/timing.md](docs/arch/timing.md); test concept in
[docs/arch/testing.md](docs/arch/testing.md).

Three pluggable layers: **HealthSource → Engine(Policy) → RecoveryDriver**, plus
the shared **PoE fabric** (`poe.py`) that owns id→port resolution + the staged
power-cycle + per-port lock/status, exposed as `necromancer.repair_poe_port` and
delegated to by the `poe_port` driver. Guards can be **linked** into groups
(`links.py`): one repairs, the rest follow + re-verify (never compete).

## Where things are (dev container)
The integration is developed inside the **ha-core dev container**
(`/workspaces/ha-core`):
- Integration package: `config/custom_components/necromancer/` (mounted live from
  the repo — editing it edits the repo).
- ha-core source: needed for `tests.common` and to run a throwaway dev HA.
- Long-lived dev token: `/tmp/dev_token.txt` (re-mint with the helper if missing).
- Dev HA API: `http://localhost:8123` (also reachable from the Windows host via the
  forwarded port).

> The repo's own `tests/`, `docs/`, `.git` are only present when the **whole repo**
> is mounted (see [docs/dev/devcontainer.md](docs/dev/devcontainer.md)). With only
> the package mounted, copy test files in to run them.

## Run it
```bash
# start dev HA (foreground; Ctrl-C to stop) — config dir is ha-core's config/
cd /workspaces/ha-core && python -m homeassistant -c ./config
# wait until GET /api/config reports state=RUNNING

# tests — real HA core, no running server needed:
PYTHONPATH=/workspaces/ha-core:/workspaces/ha-core/config \
  python <repo>/tests/test_units.py        # + test_poe.py, test_engine.py, test_integration.py
```

## Gates before every commit
- `ruff check` + `ruff format --check` (custom-component exceptions: `D10x`,
  `TID25x`, `__future__` don't apply).
- `python -m py_compile` over all modules.
- Translations symmetric: `strings.json` == `translations/en.json` ==
  `translations/de.json`; **no `{{` in any description**; every step has a
  `description`.
- `python -m script.hassfest --integration-path <pkg> --action validate` → 0 invalid.

## Live deploy (production HA, only on explicit user OK)
Production is HAOS at `192.168.1.8` (`ssh ha`, config `/homeassistant`,
necromancer entry `01KVENAK6S79VTHV3MASZHTX8M`). Deploy = `scp` the changed
files → clear `__pycache__` → `ssh ha "ha core restart"` → verify the guards load
(`docker logs homeassistant`), no errors, `status=ok`. After a Core restart the
MCP add-ons drop — drive the API via the Supervisor proxy
(`http://supervisor/core/...` + `$SUPERVISOR_TOKEN`) until they reconnect.

## Conventions / gotchas
- Match surrounding code style; comments explain *why*.
- `unavailable`/`unknown` health = `UNKNOWN`, never a fault (no false alarm).
- A disabled entity reads `unknown`, **not** `unavailable` — to simulate an
  outage, cut power / override state, don't disable.
- Auto-recovery off = the guard escalates, never acts (incl. not following a link).

## Current state
See [docs/dev/PROJECT_STATE.md](docs/dev/PROJECT_STATE.md) for the live status
(what's deployed, what's pending, open refactors).
