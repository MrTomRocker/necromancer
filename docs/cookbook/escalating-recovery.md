# Escalating recovery: gentle first, then forceful

> Try the polite fix first — a graceful restart — and only reach for the sledgehammer (a force-kill or a full add-on restart) inside the *same* recovery attempt when the gentle path didn't take.

**Concepts shown:** template guard · off/on actions & run an action · escalating recovery (`if/then`) · `wait_template` gating · `continue_on_timeout` / `continue_on_error` · health-check verify · progress notify (`necromancer.notify_guard`)
**Use it for:** Docker containers, add-ons, services — anything where a soft restart sometimes needs a hard one.

## The problem

A graceful restart is almost always the right first move: it's clean, fast, and rarely
has side effects. The trouble is that it doesn't *always* work. A wedged Docker container
ignores `turn_off`. A Zigbee bridge that's lost its radio shrugs off a soft restart. When
that happens you want something more forceful — but you don't want to lead with the
sledgehammer every time, and you don't want two separate guards racing each other.

Necromancer's [recovery strategies](../../README.md#recovery-strategies) take standard Home
Assistant script syntax, so a single recovery sequence can be smart: do the gentle thing,
**wait** to see if it worked, and only `if` it didn't, escalate. All of that happens *before*
Necromancer counts the attempt — so one "attempt" is really "graceful, then forceful if
needed". Below are two real guards built that way.

## Example 1 — restart a Docker container, with a kill-button fallback

You expose a Docker container to Home Assistant as a `switch` (via Portainer or the
Supervisor). Normally `switch.turn_off` then `switch.turn_on` restarts it cleanly. But a
wedged container sometimes ignores `turn_off` and stays `on` — so you keep a force-kill
`button` around for exactly that case.

Strategy: **Off/on actions** (`action_cycle`) — an *off* sequence, a delay, then an *on*
sequence.

Off action:

```yaml
- service: switch.turn_off
  target:
    entity_id: switch.my_container
- wait_template: "{{ is_state('switch.my_container', 'off') }}"
  timeout: 60
  continue_on_timeout: true
- if:
    - condition: template
      value_template: "{{ is_state('switch.my_container', 'on') }}"   # still up — turn_off didn't take
  then:
    - service: button.press
      target:
        entity_id: button.my_container_force_kill
      continue_on_error: true
    - wait_template: "{{ is_state('switch.my_container', 'off') }}"
      timeout: 60
      continue_on_timeout: true
```

Off/on delay: `5` s. On action:

```yaml
- service: switch.turn_on
  target:
    entity_id: switch.my_container
```

Health (template — guard is *healthy* when this renders truthy):

```jinja
{{ is_state('binary_sensor.my_container_online', 'on') }}
```

Timing (see [Timing & behaviour](../../README.md#timing--behaviour)): `debounce: 300`,
`boot_window: 300` (or more — give the app time to come up *and* report online),
`cooldown: 1800`, `max_attempts: 2`.

## Example 2 — Zigbee2MQTT: restart the bridge, then the whole add-on

Zigbee2MQTT occasionally loses its radio: devices stop reporting, link-quality sensors go
stale. Pressing the built-in **Restart** button usually re-attaches the coordinator. When
it doesn't, restarting the whole add-on is the bigger hammer.

This one fits a single sequence, so use **Run an action** (`action_call`) with a
health-check:

```yaml
- service: button.press
  target:
    entity_id: button.zigbee2mqtt_bridge_restart
- wait_template: >-
    {{ has_value('sensor.living_room_sensor_linkquality')
       and has_value('sensor.front_door_linkquality') }}
  timeout: 120
  continue_on_timeout: true
- if:
    - condition: template
      value_template: >-
        {{ not has_value('sensor.living_room_sensor_linkquality') }}   # bridge restart didn't recover the radio
  then:
    - service: hassio.addon_restart
      data:
        addon: 45df7312_zigbee2mqtt   # your z2m add-on slug
    - wait_template: >-
        {{ has_value('sensor.living_room_sensor_linkquality')
           and has_value('sensor.front_door_linkquality') }}
      timeout: 180
      continue_on_timeout: true
```

Health (template):

```jinja
{{ has_value('sensor.living_room_sensor_linkquality')
   and has_value('sensor.front_door_linkquality') }}
```

You can broaden that with a bridge-connection or MQTT-broker check
(`is_state('binary_sensor.zigbee2mqtt_bridge_connection_state', 'on')`) if you have one.
Timing: `debounce: 180`, `boot_window: 240`, `cooldown: 900`, `max_attempts: 1`.

## The clever bit

The escalation lives **inside one recovery attempt**, not across attempts. The pattern is
always the same three beats:

1. **Graceful action** — the polite fix (`switch.turn_off`, press the Restart button).
2. **`wait_template` to find out if it worked** — `continue_on_timeout: true` is essential:
   without it, a timeout *aborts* the sequence and you never reach the fallback. With it, the
   sequence keeps flowing and hands control to the `if`.
3. **`if/then` for the sledgehammer** — gated on a template that's only true when step 1
   *didn't* take (still `on`, sensor still has no value). `continue_on_error: true` on the
   forceful step keeps a flaky button-press from killing the whole run.

Because all of that is one sequence, Necromancer treats it as **one attempt**. Its own
`max_attempts` and `boot_window` verify still wrap the whole thing: after the sequence
finishes, the guard waits up to `boot_window` for *health* to read OK before declaring
success — and if it doesn't, that counts as one failed attempt, then a retry, then
`escalated`. You get a two-stage fix *and* Necromancer's outer safety net, with no second
guard and no race.

## Report progress on the guard's own channel

A two-stage recovery is exactly where a heads-up *between* the stages is useful — "the gentle
restart didn't take, reaching for the hammer" — on the **same channel the guard already
notifies on**, without hard-coding that channel into the script. `necromancer.notify_guard` runs
the guard's configured notify action with your own text. Inside a recovery sequence, target the
guard via the injected `{{ guard_entity_id }}` variable — drop it into the `if/then`, right before
the forceful step:

```yaml
  then:
    - action: necromancer.notify_guard
      target:
        entity_id: "{{ guard_entity_id }}"
      data:
        message: "Soft restart didn't take — force-killing the container"
        event: "escalating"          # optional routing key for your notify action
    - service: button.press
      target:
        entity_id: button.my_container_force_kill
      continue_on_error: true
```

The notify action receives the same variables a built-in alert does (`message`, `name`,
`event_text`, `event`, and — when you pass them — `attempt` / `max`), so your existing
notification template renders it unchanged; unset variables arrive as empty strings. Route these
progress notes away from your real alarms with the `event` key (a quiet channel vs a loud one).

## Adapt it to your setup

This is the generic "soft restart, then hard restart" shape. Drop your own pair into the
same three beats:

- **Service reload → process kill.** Reload a config entry first; if the device still reads
  dead, power-cycle it.
- **App restart → host reboot.** Press an app's restart control; if it doesn't come back,
  `shell_command`/SSH a reboot of the host it runs on.
- **Integration reload → device power-cycle.** `homeassistant.reload_config_entry` first;
  fall back to cutting power at a smart plug.

Whichever you pick: keep the graceful step first, point the `wait_template` at the same
signal your *health* template uses, and gate the forceful `then` on "the graceful step
didn't take". (For reloading the device's own integration after recovery, you may not even
need the script step — see the *Restart device integration* toggle in
[Recovery strategies](../../README.md#recovery-strategies).)

## Gotchas & variations

- **`continue_on_timeout: true` is not optional.** Leave it off and a slow recovery aborts
  the sequence at the `wait_template`, so the fallback never runs and the whole attempt is
  recorded as a failure. This is the single most common mistake with this pattern.
- **Don't double-count the boot wait.** Your in-sequence `wait_template` and Necromancer's
  `boot_window` are *both* waiting for the device. Keep the in-sequence waits short-ish
  (enough to decide "did the gentle fix work?") and let `boot_window` own the final verdict,
  so a healthy-but-slow reboot isn't scored as a failed attempt.
- **Gate the `if` on a real "still broken" signal**, not on the inverse of "started". Check
  the actual state/value the device exposes, the same way your health template does — that's
  what tells you the graceful step genuinely failed.
- **Wrap fragile forceful steps in `continue_on_error: true`.** A missing button or a brief
  Supervisor hiccup shouldn't abort the run; let it flow to the final wait and let
  `boot_window` judge the outcome.
- **`max_attempts: 1` when the sledgehammer is in the sequence.** If your forceful fallback
  already lives inside the attempt (as in Example 2), there's often little point retrying the
  same heavy action; escalate to a notification instead.
- **Three stages?** Nest a second `if/then` after the first fallback's `wait_template`, or
  reach for a real escalation chain across guards (a [supervisor guard](../../README.md#supervisor-guards-watch-other-guards))
  rather than piling everything into one sequence.
