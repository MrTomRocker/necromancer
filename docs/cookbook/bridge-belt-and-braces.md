# Recover a hub or bridge two ways at once (linked guards)

> Catch a flaky Zigbee/Hue bridge whether it goes silent on the network *or* stops driving its lights — and reboot its PoE port exactly once, no matter which alarm trips first.

**Concepts shown:** template guard · count/fuzzy health · linked guards · run an action · `repair_poe_port` service · integration reload · Health Check verify
**Use it for:** Hue / Zigbee / Z-Wave bridges and hubs — any device worth detecting two independent ways.

## The problem

A bridge behind a PoE port can fail in two unrelated ways. Sometimes it falls off the
network entirely (ping dies). Other times it answers pings happily but stops talking to its
radios, so every light it owns goes `unavailable` while the box itself looks fine. One detector
alone misses half your outages.

The fix is two guards watching the *same* bridge from different angles. But if both notice the
same dead bridge and both own the same recovery, they'd power-cycle the shared port twice. You
want two detectors and one reboot. That's exactly what [linked guards](../../README.md#linked-guards-groups) give you.

## The two guards

Both guards target the same bridge and share an identical recovery. They differ only in how they
decide the bridge is sick.

**Guard A — network ping** (template Health Source):

```jinja
{{ is_state('binary_sensor.hue_bridge_ping', 'on') }}
```

**Guard B — "are the lights actually there?"** (template, count-based so one dead bulb is fine):

```jinja
{% set ls = ['light.living', 'light.kitchen', 'light.bed', 'light.bath', 'light.hall'] %}
{{ ls | map('states') | select('eq', 'unavailable') | list | count < 5 }}
```

**Shared recovery** — both guards use the same **Run an action** strategy ([recovery strategies](../../README.md#recovery-strategies)):

```yaml
# 1. Cut + restore PoE on the bridge's port (blocks until the port cycles)
- action: necromancer.repair_poe_port
  data:
    id: ec:b5:fa:12:fd:07
# 2. Wait for the bridge to answer pings again
- wait_template: "{{ is_state('binary_sensor.hue_bridge_ping', 'on') }}"
  timeout: 120
  continue_on_timeout: true
# 3. Reconnect HA to the just-rebooted bridge
- action: homeassistant.reload_config_entry
  data:
    entry_id: 1a2b3c4d5e6f7g8h9i0j   # the Hue integration's config entry
# 4. Let radios and entities settle
- delay:
    minutes: 2
```

**Timing** (set identically on both guards):

```yaml
debounce: 480        # 8 min — a bridge reboot is disruptive; only act on a real, sustained fault
boot_window: 180     # 3 min for the bridge to come back after the port cycles
cooldown: 600        # 10 min before re-arming
max_attempts: 2
```

Then **link the two guards** to each other (the *Linked guards* section on either guard's Reconfigure).

## How linking makes them cooperate

Linking puts both guards in one group that shares a root cause. When a member starts recovering,
the rest **follow**: they freeze their own logic, wait for the repair to finish, then re-check
their own health. So the moment the bridge dies, both detectors trip — but the group elects a
single leader to run the recovery, and the other becomes a follower. The follower never touches
the port; it just waits and verifies.

## How it works

The lifecycle is the same for both guards: `ok` → fault detected → `suspect` (the **debounce**
absorbs blips) → `recovering` → `verify` (wait up to the **boot window** for health to return) →
`cooldown` → back to `ok`. After `max_attempts` failed cycles it goes `escalated`.

- **Who leads:** whichever guard trips first. On a simultaneous trip exactly one is chosen leader;
  the other follows. Only the leader runs the action sequence — so `repair_poe_port` fires once.
- **Who follows:** the other guard pauses, waits for the leader's repair to finish, then re-checks
  *its own* health. Healthy afterward → it settles into the same cooldown as the leader (silent on
  success by default, since one root-cause repair should send one notification). Still unhealthy →
  it lets the leader's result decide: leader succeeded but I'm still down means only *my* device is
  affected, so I run my own recovery; leader failed means the shared cause is unfixed, so I escalate
  too instead of piling on.

## The clever bit

- **Two orthogonal detectors.** Ping watches raw reachability; the light-count watches functional
  state. A bridge that pings but has gone deaf to its radios slips past Guard A — Guard B catches it.
  A bridge that's fully offline trips both. Neither alone covers both failure modes.
- **The `< 5` is deliberate.** `count < 5` means "fewer than all five lights are unavailable," so
  one or two flaky bulbs never trigger a bridge reboot. Only a wholesale loss — the signature of a
  bridge that stopped driving its radios — counts as a fault. Tune the threshold to how much bulb
  flakiness you tolerate.
- **Linking turns two alarms into one reboot.** Both guards trip on a dead bridge, exactly one leads
  the repair, the other follows and re-verifies. The shared PoE port is cycled once, never twice.
  And [`repair_poe_port`](../../README.md#services) **coalesces per port** anyway, so even a stray
  concurrent caller joins the in-flight cycle rather than starting a second one — belt and braces.
- **`repair_poe_port` as a primitive.** Auto-PoE bundles port-cycle + verify for you, but here you
  want a custom sequence: cycle the port, *then* reload the Hue config entry so HA reconnects to the
  freshly booted bridge. Calling the service as step 1 of a **Run an action** strategy lets you do
  exactly that — port reboot followed by an integration reload, in one recovery.

## Adapt it to your setup

Swap in your own values:

- **Ping / availability sensor** — your bridge's reachability `binary_sensor` (Guard A and the
  `wait_template`).
- **Light list** — the entities that bridge actually serves; adjust the `< N` threshold to the list
  length and your flaky-bulb tolerance (Guard B).
- **PoE port id** — the bridge's MAC, IP, or static port label that Necromancer resolves to a port
  (`repair_poe_port`'s `id`).
- **Config entry id** — the Hue (or whatever) integration's entry to reload. Find it in the URL when
  you open that integration's entry in Settings, or via the config-entries developer tools.

## Gotchas & variations

- **`continue_on_timeout: true` matters.** Without it, a slow bridge that misses the 120 s
  `wait_template` aborts the rest of the recovery, so the config-entry reload never runs. Keep it
  true, then let the `boot_window` + Health Check decide success.
- **Debounce long, not short.** 480 s is intentional — rebooting a bridge knocks out every light it
  owns, so you only want it on a genuinely stuck box, not a 30 s blip. Don't drop this to seconds.
- **Same recovery on both guards.** Linking coordinates *who acts*; it doesn't share the action.
  Give both guards the identical sequence so a follower that has to run its own recovery (leader
  succeeded, my device still down) does the right thing.
- **Auto-recovery off ≠ follow.** A guard with its `auto_recovery` switch off won't follow the
  group — if the bridge is down it just `escalated`s (alarms) without acting. Leave both armed.
- **`< 5` vs `> 0`.** Using `count > 0` (any single unavailable light = fault) makes Guard B
  hair-trigger and defeats the flaky-bulb tolerance. Keep it count-based against the full list.
- **Want a per-device confirmation?** By default a follower is silent on success. Tick *Report
  success even when this guard follows a group repair* on the follower if you want both guards to
  notify after a shared repair.
