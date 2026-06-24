# Restart a pump and put its interlocked device back the way it was

> Power-cycle a stalled device whose restart trips a safety interlock — then restore the dependent
> device, but only if it was on before and only once it's safe again.

**Concepts shown:** template guard · semantic health · off/on actions · off→on variable carry · conditional restore (`if/then`) · Health Check verify
**Use it for:** pumps, compressors — anything behind a safety interlock where a dependent device must be put back exactly as it was after a restart.

## The problem

A pool pump is meant to be running (`switch.pool_pump` on) but its power draw has collapsed to near
zero — the pump has stalled. The fix is a power-cycle. The catch: a separate safety **interlock**
automation cuts the **chlorinator** the moment the pump loses flow (no chlorine without water
moving). So restarting the pump is the easy part; the hard part is putting the chlorinator *back* —
and only if it was actually on before the restart, and only once water is really flowing again. A
naive "turn the chlorinator on at the end" would switch it on even when you'd deliberately left it
off, or before flow returns.

The clean way needs one fact carried across the power-cycle: **was the chlorinator on when we
started?** That used to mean a helper `input_boolean`. Now the off action can just remember it.

## The guard

The Health Template is the "is it doing its job?" pattern — unhealthy only when the pump is
*demanded* but isn't producing:

```jinja
{{ not (is_state('switch.pool_pump','on')
        and states('sensor.pool_pump_power_median_30s') | float(999) < 50) }}
```

Strategy **Off/on actions**, with the state captured in the off action and restored in the on
action:

```yaml
# off action — remember the chlorinator state, THEN cut the pump
- variables:
    chlor_was_on: "{{ is_state('switch.pool_chlorinator','on') }}"
- action: switch.turn_off
  target: { entity_id: switch.pool_pump }
# off_on_delay: 15 s   (the interlock turns the chlorinator off in the meantime)

# on action — restart, wait for flow, restore the chlorinator only if warranted
- action: switch.turn_on
  target: { entity_id: switch.pool_pump }
- delay: { seconds: 60 }
- if: "{{ chlor_was_on and is_state('binary_sensor.pool_flow','on') }}"
  then:
    - action: switch.turn_on
      target: { entity_id: switch.pool_chlorinator }
```

`chlor_was_on` is **set in the off action and read in the on action** — that carry-over is what
makes this work without a helper entity.

## How it works

1. Pump demanded and producing → **OK**.
2. Power collapses while the switch is on → **SUSPECT** → after the debounce → **RECOVERING**.
3. **Off action:** `chlor_was_on` captures the chlorinator's current state, then the pump switch is
   turned off. Your interlock automation independently turns the chlorinator off.
4. Wait `off_on_delay`, then the **on action:** turn the pump back on, give it 60 s to spin up and
   re-establish flow, then — *only* if the chlorinator was on before **and** `binary_sensor.pool_flow`
   now reads on — turn the chlorinator back on.
5. With the Health Check, Necromancer watches the same Health Template (up to the boot window) to
   confirm the pump is producing again → **COOLDOWN** → **OK**.

## The clever bit

- **State carried across the power-cycle — no helper entity.** `chlor_was_on` is set in the off
  action and read in the on action; Necromancer hands the off action's final variables to the on
  action. Previously you needed an `input_boolean` as a bridge; now it's one `variables:` line.
- **Interlock-aware restore.** The chlorinator comes back on *only* if it was on before
  (`chlor_was_on`) — so a deliberately-off chlorinator stays off — **and** only once flow is
  confirmed (`binary_sensor.pool_flow`) — so you never run the cell dry. The safety rule wins.
- **Semantic health.** Same "on but not producing" detection as the
  [chlorinator](chlorinator-restart.md) and [inverter](inverter-sun-check.md) recipes — a stalled
  pump still reads "on", so a plain state check would miss it.

## Adapt it to your setup

- Swap the entities: the device **switch**, a **throughput** sensor (power / flow / RPM), the
  dependent device's switch, and the **flow/ready** sensor that says it's safe to restore.
- The pattern generalises to any "restart trips an interlock" case: a compressor that drops its
  heater, a server that takes down a dependent service, a valve that closes a downstream loop.
- Capture *whatever* you need in the off action — several `variables:` are fine; they all reach the
  on action.

## Gotchas & variations

- **Capture before you cut.** Put the `variables:` step *first* in the off action — once the pump is
  off and the interlock has fired, the chlorinator's live state is no longer the "before" state.
- **Restore behind a safety check.** Gating the restore on a live flow/ready sensor (not just the
  captured flag) is what keeps the interlock meaningful — don't drop it for convenience.
- **Variables are per-attempt.** Each retry runs off→on fresh, so `chlor_was_on` is re-captured each
  time; nothing leaks between attempts.
- **Engine context too.** `attempt` and `max` are also available in both actions — e.g. settle
  longer on the second try.

See also: [Health Sources](../../README.md#health-sources) · [Recovery strategies](../../README.md#recovery-strategies) · [Timing & behaviour](../../README.md#timing--behaviour)
