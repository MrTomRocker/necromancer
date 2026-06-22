# One notification script for every guard (time-aware, multi-channel)

> Stop configuring notify targets in every guard — forward Necromancer's variables to one shared script that decides where alerts go.

**Concepts shown:** notify action variables · central notify fanout · time-aware routing · `queued` mode
**Use it for:** any deployment with many guards — consistent, multi-channel, quiet-hours-aware alerts.

## The problem

You have dozens of guards. Each one can run a [notify action](../../README.md#notifications) on
problem, recovery and escalation events — but Necromancer has **no fixed targets**, so it's tempting
to paste a `notify.mobile_app_phone` (and a TTS call, and a persistent notification) into every
single guard. Now the day you swap phones, add a tablet, or want quiet hours, you're editing twenty
guards and getting it subtly wrong in three of them.

The fix: every guard forwards the same handful of variables to **one** script, and that script owns
all the routing.

## The pattern   (guard-side notify action → one shared script)

Make every guard's notify action identical — it just hands Necromancer's resolved text straight to a
shared script. The guard stays "dumb"; it knows nothing about phones or rooms.

```yaml
- action: script.necromancer_notify
  data:
    name: "{{ name }}"
    event_text: "{{ event_text }}"
    message: "{{ message }}"
    event: "{{ event }}"
    attempt: "{{ attempt | default('') }}"
    max: "{{ max | default('') }}"
    attempts: "{{ attempts | default('') }}"
    reason: "{{ reason | default('') }}"
```

`message` is the full `"Name: text"` line, `name`/`event_text` are its parts (handy when a channel
takes a separate title), `event` is the key you can branch on (`problem_detected`, `recovery_attempt`,
`recovery_success`, `recovery_failed`, `recovery_blocked`, `no_auto_recovery`,
`linked_repair_failed`), and `attempt`/`max`/`attempts`/`reason` fill in where applicable. The
`default('')` keeps the script's fields tidy when a variable isn't set for a given event.

## The shared script   (skeleton with routing)

The script declares each variable as a `field`, recomputes the texts (so it also works if you call
it by hand), fires the always-on channels, and only adds the voice channels during the day. Run it
`mode: queued` so a burst of guard events queues up instead of dropping or overlapping.

```yaml
necromancer_notify:
  alias: Necromancer notify
  mode: queued
  max: 10
  fields:
    name: { description: Guard name }
    event_text: { description: Event text without the name }
    message: { description: Full "Name: text" line }
    event: { description: Event key }
    attempt: { description: Recovery attempt number }
    max: { description: Max attempts }
    attempts: { description: Plural-correct attempts phrase }
    reason: { description: Why a recovery was blocked / skipped }
  sequence:
    - variables:
        _name: "{{ name | default('') }}"
        _body: "{{ event_text | default('') }}"
        _full: "{{ message | default(_name ~ ': ' ~ _body) }}"

    # Always: a persistent card + your phone
    - action: persistent_notification.create
      data:
        title: "{{ _name }}"
        message: "{{ _body }}"
    - action: notify.mobile_app_phone
      data:
        title: "Necromancer — {{ _name }}"
        message: "{{ _body }}"

    # Daytime only: speak it out loud
    - if:
        - condition: time
          after: "06:00:00"
          before: "22:00:00"
      then:
        - action: notify.send_message
          target:
            entity_id:
              - notify.alexa_living_room
              - notify.alexa_kitchen
          data:
            message: "{{ _full }}"
        - action: notify.send_message
          target:
            entity_id: notify.hallway_tablet_tts
          data:
            message: "{{ _full }}"
```

The texts are already phrased for TTS (*"Recovery attempt 1 of 2."*, not *"1/2"*), so `_full` reads
naturally on Alexa and the tablet with no extra formatting.

## Why this beats per-guard notify config

- **Change once, everywhere.** New phone, a second tablet, different quiet hours? One file, not
  twenty guards.
- **Consistency for free.** Every alert is formatted and routed the same way — no guard that quietly
  forgot the TTS call or used last year's entity id.
- **Branch centrally on `event`.** Because the script gets the `event` key, you can route by
  severity in one place: send only `recovery_failed` / `no_auto_recovery` / `linked_repair_failed`
  to voice, and let routine `recovery_success` events just write the persistent card or the log.
- **The guards stay trivial.** The forwarding block is copy-paste identical, so adding a new guard
  is mechanical and there's nothing per-guard to get wrong.

## Adapt it to your setup

- **Route by `event`.** Wrap the loud channels in `{{ event in ['problem_detected',
  'recovery_failed', 'no_auto_recovery', 'linked_repair_failed'] }}` and leave
  `recovery_success` to the persistent card only — so good news doesn't announce itself in every room.
- **Add a quiet-hours / do-not-disturb switch.** Create an `input_boolean.necromancer_quiet` and add
  `condition: state` (`input_boolean.necromancer_quiet` is `off`) alongside the `condition: time`, so
  you can mute voice with one tap regardless of the clock.
- **Use real targets.** Swap `notify.mobile_app_phone`, the two `notify.alexa_*` and
  `notify.hallway_tablet_tts` for your own. `notify.send_message` with `target: entity_id: [...]` is
  the modern form for the notify *entities* your Alexa / TTS integrations expose.
- **Title vs. body.** Channels with a title field read best as `title: name`, `message: event_text`
  (no repeated name); flat channels and TTS want the whole `message` / `_full`. Both parts are in
  scope, so mix per channel.

## Gotchas & variations

- **`mode: queued`, not `single`.** A flapping device or a [linked group](../../README.md#linked-guards-groups)
  can fire several events in a second. `single` would drop them; `parallel` could interleave two TTS
  announcements. `queued` (with a sane `max`) plays them in order.
- **The notify action runs detached.** Necromancer doesn't wait for your script, so a slow TTS or a
  deliberate `delay:` inside it never stalls the engine — but it also means ordering across guards is
  best-effort, which is exactly what the queue is for.
- **Don't rely on a variable that wasn't sent.** `attempt` / `max` / `attempts` / `reason` only apply
  to some events; the `default('')` in both the guard action and the script `fields` keeps them
  blank rather than erroring. If you print them, guard with `{% if attempt %}…{% endif %}`.
- **Add channels by event severity, not by guard.** When you want a pager or a critical-alert push
  only for escalations, add it inside an `event`-based `if` in the script — never as a special-case
  notify block in one guard, or you've reinvented the problem this pattern solves.

See [Notifications](../../README.md#notifications) for the full variable list and the per-guard action format.
