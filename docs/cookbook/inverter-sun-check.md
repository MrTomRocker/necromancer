# Catch a device that's online but not doing its job (inverter sun-check)

> Your solar inverter answers every poll yet quietly produces 0 W in full sun — a reachability check says "fine", so you need a guard that asks whether it's actually *working*.

**Concepts shown:** template guard · semantic "is it doing its job?" health · notify-only · long debounce
**Use it for:** solar inverters, pumps, heat pumps — "online but idle" faults a ping can't see.

## The problem

A ping or availability check only tells you the box is reachable. A solar inverter can be perfectly
reachable — its entities update, its app connects — while it has silently dropped into a fault state
and produces **0 W in broad daylight**. Nothing is "offline", so nothing alerts you. You only find
out when the day's yield is suspiciously low.

You want to catch the *semantic* fault: bright outside, but no power coming in.

## The guard

The health is a Jinja **template** ([Health sources](../../README.md#health-sources)) that returns
`true` while everything is fine and `false` only when the inverter is genuinely stuck:

```jinja
{{ not (
     states('sensor.outdoor_brightness')|float(0) > 3000
     and states('sensor.inverter_pv_power')|float(99999) < 100
     and state_attr('weather.home','temperature')|float(99) > 3
) }}
```

Pick **Notify only** as the strategy ([Recovery strategies](../../README.md#recovery-strategies)):
a stuck inverter usually needs a human eyeball or a deliberate restart you don't want a machine doing
unsupervised. So the guard *detects* and *tells you*, but never acts.

Timing ([Timing & behaviour](../../README.md#timing--behaviour)) — give it a long debounce so
passing clouds, dawn and dusk don't fire it:

```text
Debounce: 900 s   (15 min)
```

## How it works

The template is `false` (UNHEALTHY) only when **all three** signals line up:

- `outdoor_brightness > 3000` — it's genuinely bright, so the inverter *should* be producing.
- `inverter_pv_power < 100` — but it's putting out essentially nothing.
- `weather.home` temperature `> 3 °C` — and it's not freezing, so this isn't a normal cold-morning
  standby.

The outer `not (...)` flips that: healthy whenever the "stuck in the sun" condition is *not* met.
A `false` that persists for the full 900 s debounce moves the guard into `suspect` and then fires
the notify event — you get the message, and nothing else changes.

## The clever bit

The health source asks **"is it doing its JOB?"**, not "is it reachable?". Cross-checking three
independent signals — brightness, output, and temperature — turns a fuzzy "something seems off" into
a precise, defensible condition.

The `|float(default)` choices are deliberate: every default makes an *ambiguous* reading fall on the
**healthy** side.

- brightness missing → `float(0)`, not `> 3000` → healthy
- power missing → `float(99999)`, not `< 100` → healthy
- temperature missing → `float(99)`, which is `> 3`, but the other two already failed → healthy

So a dropped sensor reading never invents a fault. And because a noisy physical signal swings
through cloud shadow and twilight, the **long 900 s debounce** is what keeps it honest: only a
sustained dead-in-the-sun state survives it.

## Adapt it to your setup

The pattern generalizes to anything that's *online but idle*:

```text
unhealthy = (it should be doing X) and (it isn't)
```

- A **pump** that should run when the tank is full: `tank_full and pump_power < 5`.
- A **heat pump** that should heat when it's cold: `indoor < target - 1 and compressor_power < 50`.
- A **driveway camera** that should see motion on a busy street: `time is daytime and
  minutes_since_last_motion > 120`.

Always wrap each reading in `|float(default)` / `|int(default)` so a missing value lands on the
healthy side, and pick a debounce long enough to outlast the normal quiet stretches of that signal.

Once you trust the detection, you can swap **Notify only** for a real
[Recovery strategy](../../README.md#recovery-strategies) — for example a **Run an action** strategy
that restarts the inverter integration or toggles its switch — and let Necromancer fix it for you.

## Gotchas & variations

- **Tune the thresholds to your hardware.** `3000` lux and `100 W` are examples; a small inverter
  idles differently from a large one. Watch the values for a few sunny days first.
- **Too short a debounce = false alarms.** A single drifting cloud can dip output for a minute. Below
  ~10 minutes you'll get noise; 15 minutes is a safe starting point.
- **Verify still works on notify-only.** The template is continuous, so the guard knows the moment
  health returns and clears itself — you won't be nagged after the inverter wakes up.
- **Watch your entity names.** `sensor.outdoor_brightness`, `sensor.inverter_pv_power` and
  `weather.home` are placeholders — substitute your own, and confirm each renders a number in
  Developer Tools → Template before saving.
- **Freezing edge case.** The `> 3 °C` guard suppresses cold-morning standby. If your inverter
  produces fine below freezing, drop that clause; if it commonly idles in cold *and* dim light, the
  brightness clause already covers you.
