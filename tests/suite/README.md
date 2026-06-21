# Necromancer pytest suite

Component- and flow-level tests that run inside the **Home Assistant core test
harness** (real `hass` fixture, `MockConfigEntry`, registries, full setup +
platform forwarding). The legacy in-process scripts in `tests/*.py` stay as fast,
server-free unit checks; this suite covers the parts they can't reach —
`async_setup_entry`, the platform entities, and the config / subentry / options
flows.

## Why the core harness (not pytest-homeassistant-custom-component)

`pytest-homeassistant-custom-component` pins a **released** HA version. This repo
is developed against an editable `ha-core` checkout (a `…dev0` version), so the
plugin would fight the installed `homeassistant`. The core test infra is the same
machinery the plugin repackages — and it's version-matched — so we use it directly.

## Layout / how it's wired

The real files live here (`tests/suite/`, source of truth, versioned with the
component). They are reached by pytest through two symlinks **in the ha-core
checkout** (not committed here):

```
<ha-core>/tests/components/necromancer            -> <repo>/tests/suite
<ha-core>/tests/testing_config/custom_components/necromancer
                                                  -> <ha-core>/config/custom_components/necromancer
```

The first puts the tests where ha-core's `tests/conftest.py` (the `hass` fixture)
applies; the second lets the `enable_custom_integrations` fixture load the
component. Recreate them after a fresh checkout:

```bash
ln -sfn <repo>/tests/suite \
        <ha-core>/tests/components/necromancer
ln -sfn <ha-core>/config/custom_components/necromancer \
        <ha-core>/tests/testing_config/custom_components/necromancer
```

> The suite directory must **not** be named `pytest` — on `sys.path[0]` it would
> shadow the real `pytest` module and break the legacy scripts.

## Running

```bash
# from the ha-core checkout, with its venv (has homeassistant + test deps)
cd <ha-core>
<venv>/bin/python -m pytest tests/components/necromancer/ -p no:cacheprovider -q -o addopts=""
```

## Coverage (this suite + the legacy scripts = full automated picture)

```bash
cd <ha-core>
SRC=<ha-core>/config/custom_components/necromancer
python -m coverage run --source=$SRC -m pytest tests/components/necromancer/ -o addopts=""
for s in test_units test_poe test_engine test_integration; do
  PYTHONPATH=<ha-core>:<ha-core>/config \
    python -m coverage run --source=$SRC -a <repo>/tests/$s.py
done
python -m coverage report --sort=cover
python -m coverage html -d <repo>/htmlcov
```

## Fixtures (`conftest.py`)

- `make_guard(name, *, strategy=…, …)` — build one guard subentry `data` dict in
  the exact shape the config flow emits (every strategy supported).
- `setup_guards(*guard_dicts, options=…)` — async factory: create a service
  `MockConfigEntry` with those guards as subentries, set it up, assert it loaded.
- `entity_id_for(hass, subentry_id, domain, key)` — resolve a guard's view-entity
  id via the registry (`status`, `health`, `auto_restart`, `recover`).

## Two flow-rejection idioms

- Handler validation (`duplicate_name`, `no_self_link`, `action_required`) →
  the flow **returns** a form with `result["errors"]`.
- Selector validation (bad Jinja, out-of-range number, missing required field) →
  `async_configure` **raises** `homeassistant.data_entry_flow.InvalidData`.
