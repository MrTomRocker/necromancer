# Necromancer — glossary & naming

Canonical names for the UI strings (`translations/en.json` + `de.json`) and the docs.
Two classes: **fixed proper nouns** (identical EN = DE) and **translated terms** (each
language reads its own word).

## 1. Fixed proper nouns — identical EN = DE, never translated

The integration's coined vocabulary and system/entity concepts. German keeps them English;
compounds hyphenate (`PoE-Port`, `Health-Check-Schalter`). The standalone term stays two
unhyphenated words (`Health Check`, `Health State`).

| Term | What it is | Translation |
|---|---|---|
| **Necromancer** | the integration | — |
| **Guard** (de „Guard") | one monitored unit; introduce as „Necromancer guard" | — |
| **Health State** | the monitored health condition (the thing detected) | — |
| **Health Template** | the Jinja-template health source | — |
| **Health Check** | the per-recovery verify toggle (`health_check`) | — |
| **Health Source** | the step that picks where the Health State comes from | — |
| **Health** | the connectivity `binary_sensor`'s name | — |
| **Auto-PoE** | the auto-PoE recovery strategy | — |
| **PoE port** (de „PoE-Port") | a switch-port entry | — |

`guard` is lowercase in English prose (common noun: `a guard with this name`); German
capitalizes it (`ein Guard`). Capitalize English `Guard` only at sentence start.

## 2. Translated terms — each language its own word (NOT fixed)

The watched thing and the process/phase words. **Rule: in each language the *description*
word must equal that language's *status/entity* name** (§3) — a German user reads the same
word in the config flow and on the status sensor.

| Concept | EN | DE |
|---|---|---|
| the watched thing | monitored device | überwachtes Gerät |
| a port's typed-in id | static label | Statisches Label |
| the repair process | Recovery | **Reparatur** |
| the settle phase | Cooldown | **Abkühlphase** |
| healthy *(adjective)* | healthy | gesund |
| on-value meaning | OK | „in Ordnung" |
| off-value meaning | faulty | „gestört" |

Three `device` senses stay distinct: the **monitored device** (what a Guard watches — use
this phrase, not bare „device"), the **assigned device** (`device_id`, the optional registry
link), and a **PoE-port device**. The **Guard** is the proper-noun actor. When no device is
assigned, the auto-created device is `manufacturer="Necromancer"`,
`model="Necromancer guard monitored device"` (set in `entity.py`, not translated).

## 3. Localized display names (status / entities / services)

Translated like any HA entity/state (German users expect German names). Descriptions use the
**same** word per language (§2).

| Element | EN | DE |
|---|---|---|
| status `ok`/`suspect`/`blind`/`recovering`/`verify`/`cooldown`/`escalated`/`snoozed` | OK / Suspect / Blind / Recovering / Verifying / Cooldown / Escalated / Snoozed | OK / Verdacht / Blind / Reparatur läuft / Prüfung / Abkühlphase / Eskaliert / Schlummert |
| switch (auto-recovery) | Auto-recovery | Auto-Reparatur |
| button (manual) | Revive | Reparieren |
| event (recovered) | Recovery | Wiederbelebung |

The **button + event** carry the Necromancer „revive" flavour (EN „Revive" / DE
„Wiederbelebung") — kept on purpose, distinct from the neutral process word „Reparatur".

## 4. Rules

1. **Never translate the §1 proper nouns.** German keeps them English; compounds hyphenate.
2. **Description word = same-language status word.** German descriptions say „Reparatur" /
   „Abkühlphase" (not „Recovery"/„Cooldown"); English says „Recovery" / „Cooldown".
3. **Markdown renders only in some slots** (verified live): the **step `description`** and a
   **field `data_description`** render Markdown (backticks → `code`); a **section
   `description`** and **field labels** are PLAIN TEXT (backticks/`\n` show literally); an
   **`ActionSelector`** field shows **no `data_description`** at all → put its hint in the
   section `description` (plain text, comma-separated tokens).
4. **Backtick literal tokens only in the Markdown slots** — HA state values (`on`, `off`,
   `unknown`, `unavailable`, `none`), Jinja (`true`, `false`, `is_state(...)`), variable names.
   The on/off value *meanings* (OK, faulty) stay prose.
5. **Action variables** — notify: `message, name, event_text, event, attempt, max, attempts,
   reason`; recovery: `attempt, max, name, guard_entity_id` (+ off→on: off-action vars
   readable in the on-action).

## Quick check

```
grep -nE "Wächter|Gesundheitszustand|Zustands-Template|Health-Prüfung|fixed id" translations/*.json   # → empty
grep -nE "Recovery|Cooldown" translations/de.json   # → only in §3 display names, not in descriptions
```
