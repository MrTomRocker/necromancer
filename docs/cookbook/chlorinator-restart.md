# Restart a chlorinator that's on but not producing

> A salt-water chlorinator reports "on" yet its output has collapsed to zero — power-cycle it
> automatically, but only once you're sure it has *really* stalled.

**Concepts shown:** template guard · semantic health (with a guard condition) · off/on actions (switch power-cycle) · smoothed/median input · Health Check verify · long cooldown
**Use it for:** salt chlorinators, pool pumps, heaters — "on but not producing", fixed by a power-cycle.

## The problem

A salt-water chlorinator (a salt-electrolysis cell) is switched on and *should* be producing
chlorine. Every so often the cell stalls: it still reads as "on" and draws power as usual, but its
production output drops to ~0. A plain "is the switch on?" check says everything is fine while the
pool slowly goes unchlorinated. You want to catch the *functional* stall and give the cell a
power-cycle — but **not** when you deliberately switched it off (winter, maintenance, a schedule).

## The guard

Health is a **template** that is unhealthy only when the device *should* be working but isn't:

```jinja
{{ not (is_state('switch.pool_chlorinator','on')
        and states('sensor.pool_chlor_power_median_30s') | float(999) < 10) }}
```

- Unhealthy ⇢ the switch is **on** *and* production (a 30-second median) is **below 10**.
- Switch off, or production unknown → **healthy**. No false alarm for "off on purpose", and a
  missing reading falls on the safe side (`float(999)` ≥ 10 ⇒ healthy).

Recovery is a plain **off/on actions** (`action_cycle`) power-cycle of the chlorinator's switch:

```yaml
# off action
- action: switch.turn_off
  target: { entity_id: switch.pool_chlorinator }
# off_on_delay: 15 s   (wait between off and on)
# on action
- action: switch.turn_on
  target: { entity_id: switch.pool_chlorinator }
- delay: { seconds: 60 }   # let the cell spin back up before the verify starts
```

Timing — deliberately patient for a chemical/hardware process:

```yaml
debounce: 600       # 10 min of sustained stall before acting
boot_window: 180    # wait for production to climb back over 10 (Health Check)
cooldown: 1800      # 30 min settle after a successful restart
max_attempts: 2
```

## How it works

1. The 30 s median sits above 10 while the cell produces → **OK**.
2. Production collapses while the switch is on → **SUSPECT**; the debounce timer starts.
3. Still stalled after **10 minutes** → **RECOVERING**: switch off → 15 s → on → 60 s settle.
4. **VERIFY**: Necromancer re-checks the same template for up to the **boot window**; once the
   median climbs back over 10 it's a confirmed recovery → **COOLDOWN** (30 min) → **OK**.
5. Two failed attempts → **ESCALATED** — the cell likely needs descaling, salt, or a real look,
   not another power-cycle. You get the alert instead of an endless restart loop.

## The clever bit

- **A guard condition *inside* the health, not just a threshold.** `is_state(switch,'on') AND
  power < 10` means "0 W" only counts as a fault *when it is supposed to be running*. Switch the
  chlorinator off for the winter and the guard stays silent — it never nags about, or power-cycles,
  an intentionally-off device.
- **Smoothed input.** Reading a **30-second median** sensor instead of the raw power rejects
  momentary dips, so one noisy sample can't trip it (the debounce handles the rest).
- **`float(999)` = innocent until proven guilty.** A missing/unknown reading defaults *high*, i.e.
  healthy — matching Necromancer's "ambiguous is never a fault" rule.
- **Patience in the timing.** A long debounce (act only on a real stall) and a long cooldown (don't
  thrash a cell that just restarted) fit a slow physical/chemical process — the opposite of
  rebooting a network device.

This is the [inverter sun-check](inverter-sun-check.md) pattern — *"is it doing its job?"* health —
but with an **actual recovery** instead of notify-only: detect the functional stall, power-cycle,
and verify production resumed.

## Adapt it to your setup

- Swap the entities for yours: a device **switch** plus a **throughput/output** sensor (power,
  flow, RPM, production rate) — a pool pump, a heater, a dehumidifier, a 3D printer.
- Feed the health a **smoothed** signal: a `statistics` (median/mean) or template sensor over the
  raw value, so a single spike doesn't matter.
- Tune the threshold (`< 10`) to "clearly not working" for your device, and the debounce to how
  long a *real* stall lasts versus a normal lull.
- Set `cooldown` to how long the device needs to stabilise after a restart — chemistry, heat and
  motors want minutes, not seconds.

## Gotchas & variations

- **Keep the "switch is on" guard.** It is what stops the guard from firing when the device is off
  on purpose — without it, every scheduled-off period would look like a fault.
- **Escalation means "stop power-cycling".** After `max_attempts`, a still-dead cell is almost
  certainly a hardware/chemistry issue (scaling, low salt). Escalated alerts you rather than
  cycling forever.
- **Try notify-only first.** If you're not yet sure the detection is clean, start with the
  *Notify only* strategy (see the [inverter sun-check](inverter-sun-check.md)), watch it for a few
  weeks, then switch the strategy to the power-cycle once you trust it.
- **Fold in a salt/flow check.** If you have a "low salt" or "no flow" sensor, `or` it into the
  health so the guard doesn't power-cycle a cell that can't produce for a *chemical* reason — that
  needs a human, not a restart.

See also: [Health Sources](../../README.md#health-sources) · [Recovery strategies](../../README.md#recovery-strategies) · [Timing & behaviour](../../README.md#timing--behaviour)
