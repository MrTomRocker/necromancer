# necromancer — "All Guards" dashboard

A ready-made, **generic** Lovelace overview for your necromancer fleet. Drop it into any dashboard —
it discovers every guard automatically (no per-device wiring).

> **Screenshots:** add images under `docs/dashboard/img/` and link them here.

## What's in it

| File | Card |
|------|------|
| [`01-fleet-status-banner.yaml`](01-fleet-status-banner.yaml) | Fleet status banner — worst state across all guards |
| [`02-status-chips.yaml`](02-status-chips.yaml) | Status chips — a live count per state |
| [`03-live-incidents.yaml`](03-live-incidents.yaml) | Live incidents — one card per guard in an active state |
| [`04-all-ok-card.yaml`](04-all-ok-card.yaml) | Green "all OK" card (hides itself when incidents exist) |
| [`05-maintenance-chips.yaml`](05-maintenance-chips.yaml) | Maintenance chips — snooze all / wake all |
| [`06-guards-popup.yaml`](06-guards-popup.yaml) | "All guards" pop-up — every guard, responsive |
| [`necromancer-dashboard.yaml`](necromancer-dashboard.yaml) | **Bundle** — all cards together (incl. a "Live incidents" heading) |

> The plain "Live incidents" heading has no standalone file — it ships only inside the bundle.

## Requirements

- **necromancer ≥ 0.2.2** — the cards read the status-sensor attributes `guard_name`,
  `health_entity`, `auto_recovery_entity`, `revive_entity`, `recover_count`, `fail_count`,
  `recover_driver`, `attempt`, `snooze_until`.
- **HACS frontend cards:**
  [Mushroom](https://github.com/piitaya/lovelace-mushroom) ·
  [bubble-card](https://github.com/Clooos/Bubble-Card) ·
  [auto-entities](https://github.com/thomasloven/lovelace-auto-entities) ·
  [config-template-card](https://github.com/iantrich/config-template-card) ·
  [layout-card](https://github.com/thomasloven/lovelace-layout-card) ·
  [card-mod](https://github.com/thomasloven/lovelace-card-mod)

## Use it

1. Install the HACS cards above and make sure necromancer ≥ 0.2.2 is running.
2. Open a dashboard → **Edit** → add a **Section** (or any vertical-stack).
3. Switch it to the **YAML / code editor** and paste the cards from
   [`necromancer-dashboard.yaml`](necromancer-dashboard.yaml) (or add the individual cards 01–06).
4. Done — nothing to configure. The cards filter on `integration_entities('necromancer')` and the
   `guard_name` attribute, so they work with **any number of guards** (including multi-subentry
   devices) out of the box.

## Customise

- **Pop-up height:** `rows` on the guard bubble-card (`3` phone / `2` desktop).
- **Responsive breakpoint:** `(max-width: 600px)` — in the grid `mediaquery` and in the
  `${ window.matchMedia(...) }` expressions.
- **Colours / labels / icons:** in each card's Jinja/JS maps.

## Good to know

- **Fully generic** — no hardcoded entity IDs; immune to renames and the `_status_N` suffix.
- **Responsive** — pop-up grid 2→1 column; per-guard buttons reflow into a 2nd row on small screens.
- Code is intentionally **multi-line / readable** so it's legible in the HA card editor.
