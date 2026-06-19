# Necromancer

[![GitHub release](https://img.shields.io/github/release/MrTomRocker/homeassistant-necromancer?include_prereleases=&sort=semver&color=blue)](https://github.com/MrTomRocker/homeassistant-necromancer/releases/)
[![License](https://img.shields.io/badge/License-MIT-blue)](#license)
[![issues](https://img.shields.io/github/issues/MrTomRocker/homeassistant-necromancer)](https://github.com/MrTomRocker/homeassistant-necromancer/issues)
![HACS](https://img.shields.io/badge/HACS-none-inactive)

<div align="center">
  <img width="70%" alt="Necromancer guards overview" src="https://raw.githubusercontent.com/MrTomRocker/homeassistant-necromancer/main/img/overview.png">
</div>

**Necromancer is a generic self-healing framework for Home Assistant.** It watches your
devices, decides — calmly — when one is actually broken, and runs a recovery: power-cycle a
switch, run an action, or auto-resolve a device to its PoE port and reboot it. It replaces the
usual pile of bespoke *"ping → reload/restart"* automations with one configurable engine,
vendor-agnostic, as an orchestrator on top of the entities you already have.

## Why Necromancer?

Devices die quietly. A Hue bridge that needs a power-cycle, an access point that drops off the
network, a camera that hangs — each one usually ends up with its own hand-written
*"if unreachable for 5 minutes, toggle this switch and hope"* automation. They're brittle, they
never verify the device actually came back, and PoE-restart tooling is vendor-locked
(UniFi / Omada / Netgear) while generic SNMP tools are port-centric with no device logic.

Necromancer is vendor-agnostic. It watches **any** health signal you already have and runs
**any** recovery you can express — and for PoE it resolves a device to the **right port
automatically** (by MAC, hostname or neighbour, even after you move the cable), cycles it, and
**confirms the device is healthy again** before calling it done.

> *Example:* a Hue bridge guarded by a ping sensor. It goes unreachable → Necromancer finds the
> PoE port it's plugged into, cuts power, waits for the port and the bridge to come back, and
> only then clears the alarm. One guard replaces the whole brittle automation.

It's built around three pluggable layers, each with a generic escape hatch so the common case
needs no custom code:

> **HealthSource** *(is it ok?)* → **Engine** *(state machine + timing)* → **RecoveryDriver** *(fix it)*

- **No false alarms.** Ambiguous health (missing entity, render error, `unknown`/`unavailable`)
  is treated as *unknown*, never as *unhealthy* — nothing gets cycled on a hunch.
- **No false success.** A recovery only counts when the action ran *and* (for `*_check`
  strategies) health verified OK afterwards.
- **Survives restarts.** Escalation, attempt counters and the per-guard auto-recovery flag are
  persisted, independent of the display entities.

## What you get per guarded device

Four pure-view entities (on their own device, or attached to an existing device via the
Battery-Notes link pattern):

| Entity | Purpose |
|---|---|
| `sensor.<guard>_status` | The state machine: `ok` / `suspect` / `recovering` / `verify` / `cooldown` / `escalated`. |
| `binary_sensor.<guard>_health` | The raw health verdict from the HealthSource. |
| `switch.<guard>_auto_recovery` | Arm/disarm automatic recovery for this guard. |
| `button.<guard>_recover` | Trigger a recovery cycle manually. |

<div align="center">
  <img width="320px" alt="Necromancer guard entities" src="https://raw.githubusercontent.com/MrTomRocker/homeassistant-necromancer/main/img/guard_entities.png">
</div>

## Health sources

How Necromancer decides whether a device is alive — both are continuous, checkable expressions,
so the verify step always works:

| Source | What it is | Healthy when |
|---|---|---|
| **State-based** | one entity's state or attribute vs on/off value lists | value is in the *on* list (e.g. a ping / reachability sensor reads `on`) |
| **Template-based** | an inline Jinja template returning `true`/`false` | the template renders truthy |

## Recovery strategies

Pick the shape that fits the device. The first three come **plain** (fire-and-forget) or **with a
health-check** (wait until the device reports healthy again before declaring success):

| Strategy | What it does |
|---|---|
| **Power-cycle a switch** | turn a switch off → wait → on (e.g. a smart plug) |
| **Run an action** | one action sequence — script, service, SSH, webhook, … |
| **Off/on actions** | an *off* action → wait → an *on* action |
| **Auto-PoE** | resolve the device to its PoE port and power-cycle it, with staged verify (port goes offline → comes back) on top of the device health-check |

Notify-only guards skip recovery entirely and just raise the event.

## Examples

Each row is **one guard** — a health source paired with a strategy:

| Goal | Health source | Strategy |
|---|---|---|
| Reboot a hung **gateway / hub** (Hue bridge, Zigbee/Z-Wave coordinator) on a smart plug | ping / reachability sensor | **Power-cycle a switch** *(with health-check)* |
| Power-cycle a device behind a **Shelly** (or any smart plug) | ping / reachability sensor | **Power-cycle a switch** — the Shelly's `switch.*` entity |
| Reboot a **PoE device** (access point, camera, IP phone) and find its port automatically | ping / reachability sensor | **Auto-PoE** |
| Redeploy a **stuck Node-RED flow** | template watching a heartbeat that stopped updating | **Run an action** → Node-RED restart/redeploy endpoint |
| Repair an **automation stuck in the wrong state** | template comparing its state to the expected one | **Off/on actions** → restart that automation |

**Stuck Node-RED flow.** Make the health a template that watches a flow's heartbeat entity, and
the recovery a REST command that redeploys it:

- *Health (template-based):* `{{ (now() - states.sensor.nodered_heartbeat.last_changed).total_seconds() < 600 }}`
  — unhealthy once the heartbeat is older than 10 minutes.
- *Recovery (Run an action):* a `rest_command` (or webhook) that hits the Node-RED admin API to
  restart the flow. Add the health-check variant so Necromancer waits for the heartbeat to resume
  before declaring success.

**Automation stuck in the wrong state.** Detect the inconsistency with a template, then restart
the automation:

- *Health (template-based):* `{{ is_state('automation.nightly_backup', 'on') }}` — or any
  expression comparing the automation to the state it *should* be in.
- *Recovery (Off/on actions):* `automation.turn_off` then `automation.turn_on` — a clean reload of
  just that automation.

## Installation

### HACS (Home Assistant Community Store)

Necromancer is **not in the HACS default store yet**, so add it as a custom repository.

<details open>
<summary>Add as a custom repository</summary>

1. Open **HACS** in Home Assistant.
2. Click the `⋮` menu in the top right and choose **Custom repositories**.
3. Add the URL `https://github.com/MrTomRocker/homeassistant-necromancer` and set the category to **Integration**.
4. Search for **Necromancer** in HACS and click **Download**.
5. Restart Home Assistant.
6. Add the integration via **Settings → Devices & Services**.

</details>

You can also use this shortcut once the repository is known to HACS:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=MrTomRocker&repository=homeassistant-necromancer&category=integration)

<details>
<summary>Manual installation</summary>

1. Copy the `custom_components/necromancer` directory from this repository into your Home
   Assistant `config/custom_components/` folder.
2. Restart Home Assistant.

</details>

**Requires** Home Assistant **2025.6** or newer.

## Configuration

Necromancer is a **single hub**: you add the integration once (no input), then add a *guarded
device* for each thing you want watched.

1. Go to **Settings → Devices & Services**, click **+ Add Integration** and search for
   **Necromancer**. Confirm — the hub is added empty.
2. On the Necromancer hub, click **Add device** to create a guard. The wizard walks you through:
   **health source → device & check → strategy → recovery/notification**.

<div align="center">
  <img width="480px" alt="Add a guarded device" src="https://raw.githubusercontent.com/MrTomRocker/homeassistant-necromancer/main/img/add_device.png">
</div>

Along the way you also set the **timing** — how long a problem must persist before reacting, how
long to wait for the device to recover, and how many times to retry before escalating. The
defaults are sensible; tune them per device.

**PoE ports** (only needed for the Auto-PoE strategy) are managed as a flat list under the hub's
**Configure** (options). Each port carries a recognizable id, a status entity, the actuator
switch to cycle, and its own timing.

### Notifications

Each guard can optionally run a **notify action** on problem / recovery / escalation. There are no
fixed targets — you provide an action and Necromancer hands it a ready-made, localized `message`
plus `name` and `event` as variables, so you decide whether and how to be notified:

```yaml
- action: notify.mobile_app_phone
  data:
    message: "{{ message }}"   # e.g. "Recovery attempt 2/3 for Hue Bridge"
```

## How it works

The engine runs a fixed state machine per guard
(`OK → SUSPECT → RECOVERING → VERIFY → COOLDOWN`, with `ESCALATED` as the dead end), debounced and
persisted. Health-check strategies wait — event-driven, up to a boot window — for health to read
OK again before declaring success; plain strategies are fire-and-forget and rely on continuous
monitoring to re-trigger.

The full design, the state machine, the health sources and the driver/strategy matrix are
documented in [`docs/arch/architecture.md`](./docs/arch/architecture.md). The test concept lives
in [`docs/arch/testing.md`](./docs/arch/testing.md).

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](./CONTRIBUTING.md). In short: fork, lint &
format with `ruff`, run the P0 regression block, and open a focused pull request.

## License

Released under the [MIT License](./LICENSE).
