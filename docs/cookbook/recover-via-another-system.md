# Recover by poking another system (MQTT, REST, SSH…)

> Restart a device Home Assistant can't touch directly by telling some *other* system to do it — and verify the fix from a health signal HA *can* see.

**Concepts shown:** template guard · run an action · cross-system action (MQTT / REST / SSH / webhook) · Health Check verify
**Use it for:** devices HA can't restart directly — recovered by poking whatever can.

## The problem

Your Wolf heating bridge feeds boiler data into Home Assistant, but every so often the
integration loses the device: `sensor.boiler_setpoint` and its siblings go to *no value*
and stay there until something restarts the bridge. HA can't restart it — there's no
switch, no PoE port, no reload that brings it back. A *separate* ioBroker instance can,
though: it owns the bridge and listens on an MQTT topic for a restart command.

So the recovery isn't "HA does something to the device." It's "HA asks ioBroker to do
something," then watches its own entities to confirm the bridge actually came back.

## The guard

A template Health Source that fails when the bridge's entities go to no-value, paired with
an [`action_call`](../../README.md#recovery-strategies) recovery that publishes the restart
command and then waits for health to return.

Health Template (guard is *healthy* when this renders `true`):

```jinja
{{ has_value('sensor.boiler_setpoint') }}
```

`has_value(...)` is false exactly when the entity is `unavailable`/`unknown` — i.e. when the
integration has lost the bridge. That's the fault we want to act on.

Strategy: **Run an action** (`action_call`) — publish to MQTT, then confirm:

```yaml
# 1. Tell ioBroker to restart the bridge
- action: mqtt.publish
  data:
    topic: "ioBroker_subscribe/Heating/restart_trigger"
    payload: "true"
# 2. Wait for the bridge to report values again
- wait_template: "{{ has_value('sensor.boiler_setpoint') }}"
  timeout: 180
  continue_on_timeout: true
```

Timing (see [Timing & behaviour](../../README.md#timing--behaviour)):

```yaml
debounce: 1200      # 20 min — these come and go; only act on a long outage
boot_window: 300    # 5 min for the bridge to restart and report values again
cooldown: 900       # 15 min before re-arming after a success
max_attempts: 2     # ask twice, then escalate
```

## How it works

1. **Detect.** `has_value('sensor.boiler_setpoint')` renders false — the integration has
   dropped the bridge. The guard goes `suspect`.
2. **Debounce.** The fault must persist for the full 1200 s. The bridge often blips out and
   comes back on its own inside a few minutes; the long debounce lets those self-heal so HA
   never bothers ioBroker for nothing.
3. **Recover.** Still dead after 20 minutes → the `action_call` fires: `mqtt.publish` drops
   `"true"` on the restart topic, ioBroker picks it up and restarts the bridge.
4. **Verify (boot_window).** The `wait_template` blocks until `has_value(...)` is truthy
   again, up to 180 s, and the guard stays in `verify` until health genuinely returns — up
   to the 300 s boot window. No false success just because the action ran.
5. **Cooldown.** Healthy in time → `cooldown` for 900 s before returning to `ok`, so it
   can't loop on a bridge that's slow to settle.
6. **Escalate.** Not healthy within the boot window → retry once more, then after
   `max_attempts` the guard goes `escalated` and alerts you instead of poking ioBroker
   forever. It clears back to `ok` the moment health returns.

## The clever bit

Necromancer doesn't need to *own* the device. There's no switch to flip, no PoE port to
resolve, no config entry to reload — and that's fine, because the recovery is **just a Home
Assistant action**. Anything HA can call can be a recovery:

- `mqtt.publish` to any broker-attached system (here, ioBroker)
- a `rest_command.*` to a vendor's REST API
- a `shell_command.*` that SSHes in and reboots a box
- a webhook to a cloud service

The only two things a guard genuinely needs are **(1) a health signal** to detect the fault
*and verify the fix*, and **(2) some callable** that triggers the restart. The `wait_template`
closes the loop: it turns "I sent a command" into "the device is actually back," using the
same `has_value(...)` health expression. And the long **debounce** is what keeps this from
hammering the other system over every transient blip — you only reach across to ioBroker for
a real, sustained outage.

## Adapt it to your setup

Keep the shape — *fault signal → callable that restarts → wait for the signal to return* —
and swap the middle step for whatever your device responds to:

- **REST API.** Define a `rest_command` for the vendor's restart endpoint and call it as
  step 1: `- action: rest_command.restart_my_bridge`.
- **SSH / shell.** A `shell_command` that runs `ssh pi@... sudo reboot` reboots a Raspberry
  Pi or any Linux host: `- action: shell_command.reboot_pi`.
- **Webhook.** `- action: rest_command.*` (or a `notify`/webhook target) kicks a cloud
  service that owns the device.
- **A different broker / topic.** Change the `topic` and `payload` to whatever your system
  subscribes to. Other systems often want JSON — `payload: '{"cmd":"restart"}'`.
- **The health signal.** Point both the [Health Source](../../README.md#health-sources) and
  the `wait_template` at whatever entity goes stale when *your* device dies — a setpoint, a
  last-seen timestamp, a ping sensor. Always pair the action with a health signal so the
  verify step can confirm the device really came back.

## Gotchas & variations

- **`continue_on_timeout: true` matters.** Without it, a slow restart that misses the
  `wait_template` timeout aborts the recovery early. Keep it true and let the `boot_window`
  plus Health Check decide success — that's what counts the attempt, not the wait.
- **Fire-and-forget vs. Health Check.** With the Health Check toggle off, an `action_call`
  runs once and trusts it worked. With it on (the default — the `wait_template` + boot-window
  verify above), you get retries and honest success/failure. Keep it on whenever you can
  observe the device.
- **Debounce long on purpose.** 1200 s is deliberate. Restarting another system is a heavy,
  external side effect — don't drop this to seconds, or every momentary blip pokes ioBroker.
- **Make the other system idempotent if you can.** With `max_attempts: 2`, a stuck bridge
  gets the restart command twice. Ensure a second "restart" while one is already in progress
  is harmless on the receiving end.
- **No health signal at all?** Then you can't verify — fall back to a fire-and-forget
  `action_call` or a **notify-only** guard, and accept that Necromancer can't confirm the
  fix or retry intelligently.
