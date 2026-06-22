# Reboot a PoE access point — without fighting the controller

> Power-cycle a hung PoE access point by its switch port automatically — but never mid-firmware-update, and never just because the controller blinked.

**Concepts shown:** template guard · Auto-PoE · health-check verify · firmware-update gate · controller-down gate
**Use it for:** PoE access points, cameras, IP phones, switches — anything you reboot by cutting its PoE port.

## The problem

A UniFi U7 Lite access point sometimes wedges: clients drop, but the AP doesn't reboot
itself. The fix is brutal but reliable — cut PoE power on its switch port and let it boot
fresh. You don't want to do that by hand, and you don't want to hard-code a port number
that breaks the day you re-patch the cable.

Two things make a naive "ping fails → cycle the port" automation dangerous:

- A power cut **during a firmware flash** can brick the AP.
- The AP's state is reported *by the UniFi controller*. If the controller goes down, every
  AP looks dead at once — and a dumb guard would power-cycle your whole fleet for nothing.

One Necromancer [Auto-PoE](../../README.md#recovery-strategies) guard, with a health
template that knows about both traps, handles all of it.

## The guard

A template-based health source paired with the Auto-PoE strategy. The AP is identified by
its **MAC**, so Necromancer resolves the right [PoE port](../../README.md#poe-ports) itself.

Health (template — guard is *healthy* when this renders truthy):

```jinja
{{ has_value('sensor.u7_lite_state')
   or (state_attr('update.u7_lite', 'in_progress') | bool)
   or is_state('binary_sensor.unifi_controller_reachable', 'off') }}
```

Strategy: **Auto-PoE**, with the AP's MAC as the device id:

```yaml
strategy: poe_port
poe_port:
  expected_id: "8c:30:66:44:11:3d"   # the U7 Lite's MAC — Necromancer finds its port
```

Timing (see [Timing & behaviour](../../README.md#timing--behaviour)):

```yaml
debounce: 600      # 10 min — AP must be down this long before we touch power
boot_window: 300   # 5 min — how long the AP gets to boot and report healthy again
cooldown: 600      # 10 min — settle before re-arming after a success
max_attempts: 2    # two port-cycles, then escalate
```

## How it works

1. **Detect.** Health renders falsy — `sensor.u7_lite_state` has no value, and neither
   safety gate is active. The guard goes `suspect`.
2. **Debounce.** The fault must persist for the full 600 s. A two-minute blip clears
   itself and nothing happens.
3. **Recover.** Still down after 10 minutes → Auto-PoE resolves the MAC to exactly one
   switch port and power-cycles it (port off → wait → on).
4. **Verify (boot_window).** Auto-PoE stages the check: it waits for the port to come back
   *and then* for health to read OK again, up to 300 s. The AP needs a couple of minutes to
   boot, so the guard stays in `verify` until it genuinely reports back — no false success.
5. **Cooldown.** Healthy in time → the guard sits in `cooldown` for 600 s before returning
   to `ok`, so it can't loop.
6. **Escalate.** Not healthy within the boot window → retry (one more attempt), then after
   `max_attempts` the guard goes `escalated` and alerts you instead of cycling forever. It
   clears itself back to `ok` the moment health returns.

## The clever bit

The whole design is in that one health template. It has three jobs:

1. **Firmware-update gate** — `or (state_attr('update.u7_lite', 'in_progress') | bool)`.
   While a firmware flash is running, this term is true, so the guard reads **healthy** no
   matter what the state sensor says. Cutting power mid-flash can brick the AP; this gate
   makes that impossible. When the update finishes, the gate drops and normal watching
   resumes.

2. **Controller-down gate** — `or is_state('binary_sensor.unifi_controller_reachable', 'off')`.
   The AP's state entity is reported *by* the UniFi controller, so a controller outage makes
   that entity go stale/unknown across every AP at once. Without this gate, one controller
   hiccup would trip every AP guard and power-cycle the entire fleet for nothing. The gate
   says, in effect, *"if the controller is the problem, then I'm fine"* — the AP isn't down,
   the reporter is. (A lightweight alternative to building a full supervisor guard.)

3. **Auto-PoE by MAC** — `expected_id: "8c:30:66:44:11:3d"`. Necromancer scans the ports and
   cycles the one currently reporting that MAC, so re-cabling the AP just works. And because
   it **remembers** the last-known port while the AP is healthy, it can still cycle the right
   port after the AP has gone fully dark and aged out of the switch's neighbour table.
   **Exactly one** port must match — zero or several blocks the recovery on purpose, so
   nothing random ever gets power-cycled.

## Adapt it to your setup

- **Find the AP's MAC.** UniFi: the device's *Details → Overview* panel, or its entity
  attributes in Home Assistant. Any format works — ids are matched trimmed and
  case-insensitive. Prefer MAC over IP if you use DHCP. Quote it in YAML (it looks like a
  number to the parser).
- **Pick the state/availability entity.** Use whatever entity goes stale when the AP is
  down — a per-AP state sensor, an uptime/last-seen sensor, or a ping sensor you run yourself.
  `has_value(...)` treats "no value" as down; swap in `is_state(...)` / `states(...)` if your
  entity reports an explicit up/down value instead.
- **Find the controller-reachable signal.** A `binary_sensor` that's `on` while the UniFi
  controller is reachable (a ping/`binary_sensor` on the controller host works well). Point
  the third term at it. No such sensor? Add a ping binary_sensor against the controller's IP.
- **Set boot_window to match the AP.** Time a real reboot from power-on to "reports healthy"
  and use that, plus margin. A U7 Lite is ~5 min; bigger APs or a slow controller can be more.
  Too short and a healthy reboot is counted as a failed attempt.

## Gotchas & variations

- **The controller gate must match how your AP entity fails.** If the controller going down
  makes the AP entity read `unavailable`/`unknown`, that's already *unknown* (never a fault),
  so the gate is belt-and-braces. But if your entity flips to an explicit "down" value when
  the controller drops, the gate is what stops a fleet-wide cycle — keep it.
- **Configure the PoE port first.** Auto-PoE needs the switch's ports defined under
  Necromancer's *Configure* — see [PoE ports](../../README.md#poe-ports). Without a matching
  port the guard goes straight to `escalated` (a `blocked` event: a wiring problem, not a
  dead AP).
- **Test it for real — don't disable entities.** A disabled entity reads `unknown`, which is
  never a fault. Pull the AP's cable or override its state to simulate a genuine outage.
- **Many APs?** Clone this guard per AP, changing only the entity names and the MAC. If you'd
  rather only act when the *whole* fleet is down, watch the AP guards' `sensor.*_status` from a
  supervisor guard instead.
- **Run it on demand.** `necromancer.repair_poe_port` with the AP's MAC as `id` power-cycles
  its port immediately — the same primitive Auto-PoE uses — handy for a dashboard button.
