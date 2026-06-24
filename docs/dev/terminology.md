# Necromancer — terminology & proper nouns

Canonical names for the UI strings (`translations/en.json` + `de.json`) and the docs.
The point: terms that name a Necromancer concept must **always read the same**, and the
**proper nouns below are identical in English and German — they are never translated.**

## Proper nouns — identical EN = DE, never translated

| Term | What it is | Do **not** write |
|---|---|---|
| **Necromancer** | the integration | — |
| **Guard** (de also **Guard**) | one monitored unit; introduce as **Necromancer guard** / de **Necromancer Guard** | „Wächter" |
| **Health State** | the monitored health condition (the thing detected) | de „Health Zustand", „Gesundheitszustand", „Zustand"; en „the state" (when it means the Health State) |
| **Health Template** | the Jinja-template health source | de „Zustands-Template"; en „State template" |
| **Health Check** | the per-recovery verify toggle (`health_check`) | en „health-check"; de „Health-Prüfung" |
| **Health Source** | the step that picks where the Health State comes from | de „Health Zustandsquelle"; en „Health source" (lowercase) |
| **Recovery** | the repair process / strategy (the act of bringing the device back) | de „Reparatur", „Wiederherstellung"; en „repair" (as the noun-concept) |
| **Health** | the connectivity binary_sensor's name | de „Gesundheit" |
| **Auto-PoE** | the auto-PoE Recovery strategy | — |
| **PoE port** (de **PoE-Port**) | a switch port entry | — |

## Rules

1. **Don't translate the proper nouns.** In German they stay English and keep the same
   capitalization (e.g. **Health State**, not „Health Zustand" or „Gesundheitszustand").
2. **German compounds hyphenate** the English term: `Health-Check-Schalter`,
   `Health-State-Quelle`, `PoE-Port`. The standalone term stays two unhyphenated words
   (`Health Check`, `Health State`).
3. **`guard` is lowercase in English** as a common noun (`a guard with this name`,
   `this guard's status entities`); German capitalizes it (`ein Guard`). Capitalize the
   English `Guard` only at the start of a sentence.
4. **`Recovery` is English in both languages** in *descriptions* (de: capitalized noun,
   `eine Recovery`, `Recovery-Strategie`). But the **status-state values and the
   entity/service display names stay localized** by deliberate choice — so German keeps
   `recovering` = „Reparatur läuft", the switch „Auto-Reparatur", the button „Reparieren",
   the event „Wiederbelebung". These are the *only* places „Reparatur"/„Wiederbelebung"
   may still appear in German.
5. **The watched thing is the *monitored device* / *überwachtes Gerät*** (translatable
   common noun). The **Guard** is the proper-noun actor; the *monitored device* is what
   it watches — use that phrase, not bare „device", when you mean the watched thing. Two
   other `device` senses stay distinct: the **assigned device** (`device_id`, the optional
   registry link) and a **PoE-port device**. `healthy`/`gesund` is the adjective; the
   on/off value meanings are `OK`/„in Ordnung" and `faulty`/„gestört".
   - Concrete UI labels: the **subentry** is a **Necromancer guard** (`entry_type` +
     the Add/Reconfigure buttons). When no device is assigned, the device Necromancer
     auto-creates has `manufacturer="Necromancer"`, `model="Necromancer guard monitored
     device"` (set in `entity.py`, not translated).
6. **Backtick literal tokens — but only where Markdown actually renders.** HA's
   config-flow form does *not* render Markdown everywhere — verified live:
   - **Field `data_description`** (the helper under a field) and the **step `description`**
     (top of the dialog) **render Markdown** → wrap literals in backticks (renders as
     `code`).
   - A **section `description`** (the text under a section header) renders **PLAIN TEXT** —
     backticks show as raw `` ` `` characters, **and `\n` line breaks collapse** (no
     paragraphs/lists possible — just one flowing block). **Don't backtick there**; list
     tokens as plain comma-separated words.
   - **Field labels** (`data`) render plain text too — never backtick them.
   - An **`ActionSelector` field** (notify/recovery actions) shows **no `data_description`
     at all**. Its hint (incl. the variable list) must live in the **section
     `description`** — i.e. plain text, no backticks.

   Tokens worth backticking *in the Markdown spots*: HA **state values** (`on`, `off`,
   `unknown`, `unavailable`, `none`, `ok`, `unhealthy`), **Jinja** literals/functions
   (`true`, `false`, `is_state(...)`), **variable / response names** (`message`, `event`,
   `attempt`, `guard_entity_id`, `health`, `timed_out`, …). The on/off value *meanings*
   (OK, faulty) stay prose.

   **Action variables** (for the section-description lists): notify action →
   `message, name, event_text, event, attempt, max, attempts, reason`; recovery action →
   `attempt, max, name, guard_entity_id` (+ off→on: off-action vars readable in on-action).

## Terms that *do* translate (common nouns / natural language)

| EN | DE | note |
|---|---|---|
| device | Gerät | the watched thing (≠ the Guard) |
| entity | Entität | |
| recovery / repair | Wiederherstellung / Reparatur | |
| healthy *(adjective)* | gesund *(adjective)* | the device **is** healthy/gesund; the **noun** is always **Health State** |
| OK *(on-value meaning)* | „in Ordnung" | the value that means healthy |
| faulty *(off-value meaning)* | „gestört" | the value that means broken |

So three distinct roles, kept separate: the concept is **Health State** (proper noun),
the adjective is *healthy / gesund*, the on-value label is *OK / „in Ordnung"*.

## Quick check

```
grep -nE "Health Zustand|Gesundheitszustand|Zustands-Template|Health-Prüfung" translations/*.json   # → should be empty
```
