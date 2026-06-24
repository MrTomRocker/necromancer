# Staged recovery, verified by the guard itself

> Do the gentle fix, ask the guard "are you healthy yet?", and only escalate to the hard fix if it isn't — re-using the guard's own health-check instead of rebuilding it in the script.

**Concepts shown:** run an action · staged recovery (`if/then`) · `necromancer.wait_for_health` / `check_health` · re-use the guard's health-check · `repair_poe_port` · progress notify · one parametrized script for many guards
**Use it for:** bridges/hubs and the devices behind them — anything where you want a soft try before a power-cycle, and you've already told Necromancer what "healthy" means.

## The problem

A two-stage recovery has to know, *between* the stages, whether the gentle fix worked. The
obvious way is a `wait_template` that re-checks the device (see
[Escalating recovery](escalating-recovery.md)) — but now "is it healthy?" lives in **two**
places: the guard's health source *and* the script's `wait_template`. They drift. Change the
health definition and the script still checks the old thing — and `wait_template` +
`continue_on_timeout` is easy to get subtly wrong.

Necromancer already evaluates the guard's health, so let the script ask *it*. Two response
services re-use the guard's own verdict — you pass the guard's status entity, the same
`{{ guard_entity_id }}` every recovery action already receives:

- `necromancer.check_health` → `{ health: ok | unhealthy | unknown }` right now.
- `necromancer.wait_for_health` → wait until healthy (or a `timeout`, default = the guard's
  boot window), returning `{ health, timed_out, waited_s }`.

## The recipe — gentle nudge, then a PoE reboot, verified each step

The guard watches a device behind a bridge; the bridge hangs sometimes and the device drops.
Strategy: **Run an action** (`action_call`) pointing at one reusable script.

Health (template — whatever "this device is alive" means to you):

```jinja
{{ has_value('cover.patio_awning') }}
```

Recovery action — call the script, passing the guard and the device specifics:

```yaml
- action: script.staged_device_recover
  data:
    guard: "{{ guard_entity_id }}"          # the guard's own status entity
    ping_button: button.patio_awning_ping   # optional gentle nudge
    bridge_mac: "b0:1f:81:b0:f4:84"         # for repair_poe_port
```

The script — one parametrized, reusable staged recovery:

```yaml
staged_device_recover:
  fields:
    guard: { description: "The guard's status entity ({{ guard_entity_id }})" }
    ping_button: { description: "Optional button to nudge the device first" }
    bridge_mac: { description: "MAC of the bridge, for repair_poe_port" }
  sequence:
    # Tier 1 — gentle: nudge the device, then ask the guard if that was enough.
    - if: "{{ ping_button is defined and ping_button }}"
      then:
        - action: button.press
          target: { entity_id: "{{ ping_button }}" }
    - action: necromancer.wait_for_health
      data:
        guard: "{{ guard }}"
        timeout: 20                 # short — just "did the nudge work?"
      response_variable: t1
    # Tier 2 — only if still not healthy: power-cycle the bridge's PoE port.
    - if: "{{ t1.timed_out }}"
      then:
        - action: necromancer.notify_guard
          target: { entity_id: "{{ guard }}" }
          data:
            message: "Gentle nudge didn't take — rebooting the bridge port"
            event: "escalating"
        - action: necromancer.repair_poe_port
          data: { id: "{{ bridge_mac }}" }
        - action: necromancer.wait_for_health
          data: { guard: "{{ guard }}" }      # no timeout -> the guard's boot window
          response_variable: t2
```

Necromancer still wraps the whole thing in its usual VERIFY: after the script returns it waits
up to `boot_window` for health and counts / retries / escalates as always. So the script's
`wait_for_health` calls are about *deciding between tiers*, not the final verdict.

## Why this beats a rebuilt `wait_template`

- **One definition of healthy.** The script never restates "is it alive?" — it asks the guard,
  which uses your health source. Change the health template once; the script follows.
- **`timed_out` is the tier gate.** No `continue_on_timeout` footgun: `wait_for_health` always
  returns, and `t1.timed_out` is a clean "the gentle fix didn't take".
- **Short tier 1, full tier 2.** Give tier 1 a small `timeout` (just to decide); leave tier 2
  with no timeout so it inherits the guard's `boot_window` — the same patience the guard's own
  VERIFY uses (so a slow-but-fine reboot isn't misread as a failure).

## One script, many guards (the blueprint)

Because the script takes the guard plus a couple of parameters, **one**
`staged_device_recover` serves every guard of the same shape: point each guard's *Run an
action* at it with that guard's `{{ guard_entity_id }}`, its own ping button (or omit it — e.g.
tubular-motor covers, where a ping would jog the motor), and the relevant bridge MAC. Add a
device, reuse the script — no per-guard recovery logic to copy.

## Gotchas & variations

- **Pass `{{ guard_entity_id }}`, not a hard-coded entity.** It's injected into every recovery
  action; that's what makes the script reusable *and* points the health-check at the right guard.
- **`check_health` for an instant branch.** When you don't need to wait — "skip tier 2 if some
  condition says don't" — call `check_health` and branch on its `health` immediately, no wait.
- **Don't ping everything.** Some devices misbehave when polled (a tubular-motor cover jogs on a
  ping; a sleepy Zigbee node wakes oddly). Make tier 1 optional, as above.
- **Tier 2 can be anything.** `repair_poe_port`, `hassio.addon_restart`, a `shell_command`/SSH —
  the shape is the same: heavy action → `wait_for_health` (default boot window) → let
  Necromancer's VERIFY own the final verdict.
- **The recovery still needs a health-check on.** `wait_for_health` reads the guard's health; keep
  the guard's own *health-check* toggle on so Necromancer's VERIFY agrees with the script.
