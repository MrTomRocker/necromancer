# Alert when a Node-RED flow (or any data source) silently stalls

> A heartbeat sensor keeps existing with a stale value when the flow behind it dies, so a plain state check never notices — watch *when it last wrote* instead.

**Concepts shown:** template guard · freshness watchdog (`last_reported`) · history/timestamp access · notify-only (or run an action + Health Check) · flow redeploy
**Use it for:** Node-RED flows, scrape / MQTT feeds, integration polls — any source that should keep updating.

## The problem

You have a Node-RED flow that writes a sensor every few minutes — a heartbeat. The
value itself doesn't matter; what matters is that the flow keeps *writing*. If the
flow crashes (or a deploy hangs, or an upstream feed dries up), the sensor stops
updating. But here's the trap: `sensor.nodered_flow_heartbeat` still exists, still
holds its last value, and is *not* `unavailable`. A guard built on the entity's state
sees a perfectly ordinary number and stays happily `ok`. The flow is dead and nobody
gets told.

You want to be alerted the moment the heartbeat goes quiet — not when the value
changes to something wrong, but when it stops arriving at all.

## The guard

A template-based Health Source that asks "was this written recently?", paired with a
notify-only strategy.

**Health (template-based):**

```jinja
{{ (now() - states.sensor.nodered_flow_heartbeat.last_reported) < timedelta(minutes=15) }}
```

**Strategy:** Notify only (`noop`) — detect and tell me, don't touch anything.

**Timing:**

```text
Debounce: 120 s     # one missed write is a blip; don't cry wolf
```

(Boot window, cooldown and max attempts don't apply to a notify-only guard — there's
no recovery action to wait on.)

## How it works

The template renders `true` while the heartbeat has been *reported* within the last
15 minutes, and `false` once it goes quiet. Necromancer evaluates it continuously
(see [Health Sources](../../README.md#health-sources)).

When it flips to `false`, the guard enters `suspect` and starts the **debounce**
timer. If the heartbeat resumes within 120 s, it was a blip — the guard slides back to
`ok` and nothing fires. If it's still quiet after the debounce, the guard raises the
problem event and runs your notify action. That's the whole lifecycle for a
notify-only guard: detect → debounce → notify (see
[Timing & behaviour](../../README.md#timing--behaviour)).

## The clever bit

The health is based on **data freshness, not value**. The single load-bearing choice
is `last_reported` rather than `last_changed`:

- `last_changed` only moves when the *value* changes. A heartbeat that writes the same
  number every two minutes would look frozen — `last_changed` could be hours old while
  the flow is perfectly alive. You'd get false alarms.
- `last_reported` moves on **every write**, even when the value is identical. That's
  exactly what "is the flow still running?" means.

No ping, no extra integration, no companion `last seen` sensor. You're reading metadata
Home Assistant already tracks on every state object. The same pattern guards anything
that *should keep updating*: an MQTT feed, a scrape sensor, a polling integration, a
cron script's output.

## Variation: auto-recover, don't just alert

If you can restart the flow programmatically, swap notify-only for **Run an action
with Health Check**. Necromancer runs your recovery action, then waits up to the
**boot window** for the *same* freshness template to read `true` again before declaring
success — so "fixed" means the heartbeat genuinely resumed, not just "the restart call
returned 200".

First define a `rest_command` that hits the Node-RED admin API (in
`configuration.yaml`):

```yaml
rest_command:
  nodered_redeploy:
    url: "http://nodered.local:1880/flows"
    method: POST
    headers:
      Node-RED-Deployment-Type: full
    # add an Authorization header if your admin API is secured
```

Then build the guard with the **same freshness template** as the Health Source, and
this recovery:

```yaml
# Recovery: Run an action (with Health Check)
- action: rest_command.nodered_redeploy
```

```text
Debounce:    120 s
Boot window: 90 s     # how long the flow needs to come up and write once more
Max attempts: 2
```

Now: heartbeat goes quiet → debounce → redeploy → Necromancer watches the template
for up to 90 s. Heartbeat resumes inside the window → `recovered`. Still silent after
two attempts → `escalated`, and it pings you (set a notify action) instead of
redeploying forever. See [Recovery strategies](../../README.md#recovery-strategies).

## Adapt it to your setup

- **Freshness window.** Set it to a bit *more* than the flow's normal update interval.
  A flow that writes every 5 minutes wants ~15 min (room for two missed writes); a feed
  that updates hourly wants ~75–90 min. Too tight and a slow tick looks like a death.
- **`last_reported` vs `last_changed`.** Pick deliberately. Use `last_reported` for a
  heartbeat that may repeat values (the usual case). Use `last_changed` only when the
  *value* itself is meant to move on every update and a frozen value is the failure.
- **Any "should keep updating" entity.** Point the template at any sensor that's
  supposed to refresh on a schedule — swap the entity id and the window and you're done.

## Gotchas & variations

- **`last_reported` is a `datetime`, not a number.** Subtracting two datetimes gives a
  `timedelta`, so compare against `timedelta(minutes=15)` — don't compare to `900`. If
  you prefer a plain number, use `.total_seconds()` on both sides.
- **The sensor must already exist.** On a fresh HA restart, `states.sensor.x` is `None`
  until the flow writes once, and `None.last_reported` raises. A brand-new heartbeat
  reads as *unknown*, not *faulty*, so Necromancer won't false-alarm — but if you want
  belt-and-braces, guard with
  `{{ states.sensor.nodered_flow_heartbeat.last_reported is not none and (now() - states.sensor.nodered_flow_heartbeat.last_reported) < timedelta(minutes=15) }}`.
- **Notify text comes for free.** A notify-only guard still hands your notify action the
  resolved `{{ message }}` / `{{ name }}` / `{{ event_text }}` variables, so you can fire
  a phone push or a TTS line without writing the wording yourself.
- **Watch the auto-recover loop.** With the redeploy variation, if the flow is broken in
  a way a redeploy can't fix, Necromancer stops after max attempts and goes `escalated`
  rather than hammering the API. It clears back to `ok` on its own once the heartbeat
  returns.
