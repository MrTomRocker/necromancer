# Necromancer — Agent-Regressions-Checkliste

Diese Checkliste ist dafür gemacht, von einem **Agenten** ausgeführt zu werden — via **Files**
(Quelldateien unter `custom_components/necromancer/` lesen + Symbole/Zeilen bestätigen) und via **API**
(eine laufende HA-Instanz mit dem Live-Test-Helfer treiben). Jeder Test ist ein abhakbarer Block mit
*Prüft / Files / Treiber / Assert / Cleanup*.

## Voraussetzungen
- **Laufende HA-Instanz** mit eingebundener Integration; `N.g("/api/config")["state"] == "RUNNING"`.
- **Live-Test-Helfer** `necro_testkit.py` (REST/WS-Treiber gegen die laufende Instanz) stellt die `N.*`-API:
  `g · st · setstate · call · log · guard · create_guard · delete_subentry · list_subentries · add_port ·
  remove_port · wait · hub_id`. Import: `import necro_testkit as N`.
- **Code-Änderung (.py) → voller HA-Neustart** (Reload reicht nicht); danach auf RUNNING warten
  (Python-Sleep bzw. `N.wait`, kein bash-`sleep`).
- **Automatisierte Suiten** (Vorbedingung — müssen grün sein, ersetzen den Pflicht-Handlauf):
  `tests/test_units.py`, `tests/test_poe.py`, `tests/test_engine.py`, `tests/test_integration.py` — mit dem
  Test-Venv und `PYTHONPATH=<ha-core>:<ha-core>/config` fahren. Sie decken automatisch ab: PoE
  resolve/cycle/coalescing/Platzhalter/Stale-Cache, Engine-State-Machine + Linking/Lifecycle +
  Persistenz-Kernpfade, Health-Registry-Events, PoE-Pre-Flight. Die `[ ]`-Punkte hier sind der
  Live-Smoke-Test obendrauf.
- **Test-Helfer-Entities** (Dev-Setup): `input_boolean.test_1..6`, `sim_poe_port`, `sim_device_power`,
  `input_select.test_state`, `input_text.test_note`, `switch.test_template_switch`,
  `binary_sensor.test_reachable`, `sensor.test_device_info`.
- **Entity-Slugs** (de-Instanz): `sensor.<slug>_status`, `binary_sensor.<slug>_gesundheit`,
  `switch.<slug>_auto_reparatur`, `button.<slug>_reparieren`; `slug = Name.lower()`.
- **Beobachten:** schnelle States (RECOVERING/VERIFY) im **Log** asserten (`N.log()`), langsame
  (SUSPECT/COOLDOWN/OK/ESCALATED) per `N.guard("<slug>")`. **Heil-Trick:** Recovery-Aktion macht die
  Health-Entity wieder gesund → VERIFY gelingt; schreibt sie nur `input_text.test_note` → VERIFY-Timeout →
  Eskalation. **Jeder Test räumt seine Guards/Ports selbst weg.**

## Konventionen für Linking-Tests
- Bei zwei verlinkten Guards bestimmt die **Erstellungs-/Debounce-Reihenfolge**, wer **Leader** wird (wer
  zuerst durch den Debounce kommt) und wer **Follower** — das ist korrektes, by-design-Verhalten. Asserts
  daher **rollen-agnostisch** schreiben: Marker über **beide** Guards prüfen und „**genau ein** echter
  `recovery attempt 1/` insgesamt", statt einen festen Guard als Follower anzunehmen. Wer einen festen
  Leader braucht, erzwingt ihn (nur dessen Health zuerst brechen, oder `button.<slug>_reparieren` drücken).

Priorität: **P0** = nach Refactors zwingend · **P1** = wichtig · **P2** = Kür. `[ ]` = beim Lauf abhaken.

> **Flow-Hinweis (Mode-Wahl entfernt):** Der Device-Step trägt **kein** `mode`-Feld mehr. Manuelle Flow-Treiber
> (`N._post_flow(fid,{...})`) dürfen `"mode"` NICHT mehr im Device-Step posten — sonst `extra keys not allowed
> @ data['mode']`. Die Strategie-Wahl kommt erst im **nächsten** Step (`strategy`): `"notify"` (erste Option,
> → Notify-Step) oder eine Recovery-Strategie (`switch`/`action`/`actions`/`poe_port` → eigener Step). Der
> Health Check ist kein Strategie-Variant mehr, sondern ein Toggle (`health_check`, default an) in der
> Behaviour-Section. `N.create_guard({...,"mode":"recover"|"notify",...})` bleibt unverändert gültig — das
> Testkit übersetzt `mode` in die Strategy-Wahl und akzeptiert das alte `*_check`-Kürzel weiter (→ Basis-
> Strategie + `health_check: true`).
> **Device-Step ist FLACH (section-los):** Die Felder `name`, `entity_id`, `attribute`, `on_value`,
> `off_value` (state-based) bzw. `template` (template-based) sowie `device_id` liegen alle auf **Top-Level**
> (Helper `_health_fields`, `_device_schema`). Es gibt **keine** Sections `state_check`/`template_check`/
> `assigned_device` mehr (die Konstanten `SECTION_STATE`/`SECTION_TEMPLATE`/`SECTION_DEVICE` wurden entfernt).
> Das Testkit (`create_guard`) postet aus Altgründen noch verschachtelt (`{"state_check":{...}}` etc.); der Flow
> zieht das via `_flatten_sections` hoch, daher funktioniert es weiter — manuelle Treiber dürfen aber genauso gut
> flach posten (`{"name":..., "entity_id":..., "on_value":[...], "off_value":[...]}` bzw.
> `{"name":..., "template":"..."}`; `device_id` top-level).
> Ist im Device-Step ein Gerät (`device_id`) gesetzt, ist im Recover-Step zusätzlich die Section `"reload":{}`
> **pflicht** (sonst `required key not provided`). (Der Recover-Step behält seine Sections
> `recovery_action`/`behavior`/`notification`/`linked_guards`/`reload` — nur der Device-Step ist flach.)

---

## Refactor-Regressionen — PoE-Stale-Cache (B1) · Linking-Teardown (B2) · LinkCoordinator (M1)

### B1 — PoE-Stale-Cache zykelt nie das falsche Gerät

- [ ] **B1.1 — resolve_with_reason: belegter last-known-Port wird verworfen** · `P0`
  - **Prüft:** Bei 0 Live-Matches vertraut die Fabric dem gecachten Port NUR, wenn dieser gerade nichts meldet; meldet er eine andere Live-id, wird der Cache-Eintrag verworfen und mit „no port matches" abgelehnt (kein Reboot des unschuldigen Nachbarn).
  - **Files:** `custom_components/necromancer/core/poe.py` → `resolve_with_reason`: Zweig `port = self._by_label(self._cache.get(target))` → `occupant = _norm(self._port_id(port))`; `occupant is None` → return cached port (WARNING „not in any port's neighbour data — last-known port"); sonst WARNING „last-known port %r for %r now serves %r — dropping stale cache", `self._cache.pop(target, None)`, return `None, "no port matches '…'"`. Bestätige, dass es KEINEN unbedingten `return port` mehr gibt.
  - **Treiber:** Referenz-Unittest fahren: `Suite `tests/test_poe.py` fahren (Voraussetzungen), Zeile `ok    test_resolve_last_known_skips_occupied_port` suchen`
  - **Assert:** Zeile `ok    test_resolve_last_known_skips_occupied_port` UND Suite endet mit `16 passed, 0 failed`. Test (test_poe.py) belegt: A war auf P1 gecacht (`cache={"aa:aa":"P1"}`), `sensor.nb1` meldet jetzt `mac=bb:bb` → `p is None`, `"no port matches" in reason`, `f.cache.get("aa:aa") is None` (stale gedroppt).
  - **Cleanup:** — (In-Process-Suite, kein Live-Guard)

- [ ] **B1.2 — Live: gecachter, jetzt fremd-belegter Port wird nicht zyklt** · `P1`
  - **Prüft:** Ein poe_port-Guard, dessen Gerät einst auf einem Port gelernt wurde, der jetzt eine andere reale id meldet, eskaliert (über `can_recover`-Block) statt den Port zu cyclen.
  - **Treiber:**
    1. Hinweis: `N.add_port(...)` im Testkit kennt KEIN `id_attribute` — die Port-id kommt aus dem **State** des `id_entity`. Daher die id über den State von `sensor.test_device_info` setzen (nicht über ein `mac`-Attribut): `N.setstate("sensor.test_device_info","aa:aa")`. Aktuator-Sim `N.setstate("switch.test_template_switch","on")`, Online-Status-Sim `N.setstate("binary_sensor.test_reachable","on")`.
    2. Testport anlegen: `N.add_port({"label":"PB1","actuator":"switch.test_template_switch","id_entity":"sensor.test_device_info","status_entity":"binary_sensor.test_reachable","off_on_delay":1,"off_timeout":5,"on_timeout":5})` → id `aa:aa` (= aktueller State) wird gelernt.
    3. poe_port-Guard erstellen (Health zuerst gesund: `N.call("input_boolean","turn_on",entity_id="input_boolean.test_6")`): `hub, sub = N.create_guard({"source_type":"state_based","name":"StaleB1","health":{"entity_id":"input_boolean.test_6","on_value":["on"],"off_value":["off"]},"mode":"recover","strategy":"poe_port","expected_id":"aa:aa","behavior":{"debounce":2,"cooldown":3,"boot_window":5,"max_attempts":1}})`.
    4. Port auf andere reale id umkabeln: `N.setstate("sensor.test_device_info","bb:bb")` (relearn läuft via state-change des id_entity), dann `N.wait(1)`.
    5. Health brechen → Recovery anstoßen: `N.call("input_boolean","turn_off",entity_id="input_boolean.test_6")`, `N.wait(5)`.
  - **Assert:** `N.guard("staleb1")` → state `escalated`. Im `N.log()`: WARNING-Marker `last-known port 'PB1' for 'aa:aa' now serves 'bb:bb' — dropping stale cache` UND der Block-Marker des poe_port-Drivers `PoE aa:aa: no port matches 'aa:aa'` (core/drivers/poe_port.py, `can_recover` blockt VOR `repair()` → Engine eskaliert via `recovery_blocked`). Der `repair()`-Marker `cannot repair 'aa:aa'` wird NICHT erwartet (repair wird nie erreicht). Kein erfolgreicher Cycle des Ports nach dem Umkabeln.
  - **Cleanup:** `N.delete_subentry(hub, sub)`; `N.remove_port("PB1")`

### B2 — Linking-Teardown ist race-sicher

- [ ] **B2.1 — Follow-up-Verify als _cycle_task: Button während Follower-Verify ignoriert** · `P0`
  - **Prüft:** Die nach Leader-Repair gestartete `validate_after_repair` läuft als `engine._cycle_task`, sodass der Busy-Guard greift: ein manueller Recover (Button) mitten im Follower-Verify wird verworfen — kein zweiter, konkurrierender Cycle.
  - **Files:** `custom_components/necromancer/core/links.py` → `on_partner_repair_done`: `eng._cycle_task = eng.hass.async_create_task(self.validate_after_repair(...))`; `validate_after_repair` setzt `GState.VERIFY`, im `finally` `eng._cycle_task = None`. `core/engine.py` → `async_manual_recover`: `if self._busy(): return`; `_busy` = `self._cycle_task is not None and not self._cycle_task.done()`.
  - **Treiber:** Referenz-Engine-Test fahren: `Suite `tests/test_engine.py` fahren (Voraussetzungen), Zeile `ok    test_validate_after_repair_blocks_manual_recover` suchen`
  - **Assert:** Zeile `ok    test_validate_after_repair_blocks_manual_recover` + Suite `34 passed, 0 failed`. Test (test_engine.py) belegt: während VERIFY `e2._busy()` True, `async_manual_recover()` → `d2.calls == 0` (kein konkurrierender Cycle), nach Heilung `e2.state is GState.COOLDOWN`, `recover_count == 1`, `d2.calls == 0`.
  - **Cleanup:** —

- [ ] **B2.2 — async_stop bricht Follower-Verify ab, keine Eskalation** · `P0`
  - **Prüft:** Stop/Unload mitten im Follower-Verify canceled die `validate`-Task sauber: kein terminaler State auf der abgebauten Engine, Link-State zurückgesetzt.
  - **Files:** `core/engine.py` → `async_stop`: zuerst `self._stopping = True`, `self.links.reset()`, am Ende `if self._cycle_task and not self._cycle_task.done(): self._cycle_task.cancel()`. `core/links.py` → `validate_after_repair` `finally` leert `_cycle_task` auch beim Cancel. `reset()` setzt `following=False`, `leader=None`.
  - **Treiber:** `Suite `tests/test_engine.py` fahren (Voraussetzungen), Zeile `ok    test_async_stop_cancels_validate_no_escalation` suchen`
  - **Assert:** Zeile `ok    test_async_stop_cancels_validate_no_escalation`. Test (test_engine.py) belegt nach `async_stop` im VERIFY: `e2.state is not GState.ESCALATED`, `e2._following is False`, `e2._stopping is True`, `not e2._busy()`, und nach `async_block_till_done` weiterhin nicht ESCALATED (keine späte Mutation).
  - **Cleanup:** —

- [ ] **B2.3 — Leader-Stop eskaliert den Follower nicht** · `P0`
  - **Prüft:** Wird der Leader mitten im Recover-Cycle gecancelt (Reload/Unload), feuert sein `finally` KEIN „done(failed)" an die Gruppe — der Follower bleibt haltend statt fälschlich zu eskalieren.
  - **Files:** `core/engine.py` → `_run_recovery_cycle` `finally`: `if not self._stopping: self.links.notify_done(self.state == GState.COOLDOWN)` — beim Stop also übersprungen. `core/links.py` → `notify_done` ruft sonst `partner.links.on_partner_repair_done`.
  - **Treiber:** `Suite `tests/test_engine.py` fahren (Voraussetzungen), Zeile `ok    test_leader_stop_does_not_escalate_follower` suchen`
  - **Assert:** Zeile `ok    test_leader_stop_does_not_escalate_follower`. Test (test_engine.py) belegt: Leader in `recover()` blockiert, Follower `_following True`/`RECOVERING`; nach `e1.async_stop()` → `e2.state is not GState.ESCALATED` und `e2._following is True` (nie benachrichtigt → hält weiter).
  - **Cleanup:** —

- [ ] **B2.4 — Live-Happy-Path: Follower folgt, eigener Cycle = 0, Erfolg via Linked-Repair** · `P1`
  - **Prüft:** Zwei verlinkte Guards: Leader geht in Recovery, Follower folgt (state RECOVERING, 0 eigene Versuche), und wird durch die geteilte Reparatur gesund (COOLDOWN wie der Leader), nicht durch einen eigenen Cycle.
  - **Treiber:**
    1. Follower (action_check, heilt sich nie selbst — Aktion schreibt nur Note): `f_entry, f_sub = N.create_guard({"source_type":"state_based","name":"LinkFollowX","health":{"entity_id":"input_boolean.test_1","on_value":["on"],"off_value":["off"]},"mode":"recover","strategy":"action_check","action":[{"service":"input_boolean.turn_on","data":{"entity_id":"input_boolean.test_1"}}],"behavior":{"debounce":2,"cooldown":3,"boot_window":4,"max_attempts":1}})`.
    2. Leader (action_check, heilt sich UND verlinkt auf Follower): `hub, leader_sub = N.create_guard({"source_type":"state_based","name":"LinkLeadX","health":{"entity_id":"input_boolean.test_1","on_value":["on"],"off_value":["off"]},"mode":"recover","strategy":"action_check","action":[{"service":"input_boolean.turn_on","data":{"entity_id":"input_boolean.test_1"}}],"behavior":{"debounce":2,"cooldown":3,"boot_window":4,"max_attempts":1},"linked_guards":[f_sub]})`.
    3. Beide Health gleichzeitig krank: `N.call("input_boolean","turn_off",entity_id="input_boolean.test_1")`; `N.wait(2)`.
    4. Leader-Aktion heilt test_1; Follower folgt → der Follower wird durch DIESELBE Health gesund; `N.wait(5)`.
  - **Assert:** `N.guard("linkleadx")` → `cooldown` (dann `ok`), Attr `recover_count == 1`. `N.guard("linkfollowx")` → `cooldown`/`ok`, Attr `recover_count == 1`, aber eigener `attempt` 0. Im `N.log()`: INFO-Marker `following (hold, verify after)` UND `healthy after linked-guard repair`. KEIN `recovery attempt 1/` für „LinkFollowX".
  - **Cleanup:** `N.delete_subentry(hub, leader_sub)`; `N.delete_subentry(f_entry, f_sub)`

### M1 — LinkCoordinator-Extraktion ist verhaltenserhaltend

- [ ] **M1.1 — core/links.py hat LinkCoordinator, core/state.py hat GState, engine re-exportiert** · `P0`
  - **Prüft:** Das Link-Runtime-Protokoll lebt in `LinkCoordinator` (core/links.py), `GState` in `core/state.py`; core/engine.py importiert beide und nutzt `self.links.*` statt Partner-Internas.
  - **Files:** `custom_components/necromancer/core/links.py` → `class LinkCoordinator` mit `find_repairing_partner`/`notify_start`/`notify_done`/`on_partner_repair_start`/`on_partner_repair_done`/`validate_after_repair`/`reset`. `custom_components/necromancer/core/state.py` → `class GState(StrEnum)`. `core/engine.py`: `from .links import LinkCoordinator`, `from .state import GState`, `self.links = LinkCoordinator(self, linked_guards, engines)`.
  - **Treiber:**
    - `grep -n "class LinkCoordinator" custom_components/necromancer/core/links.py`
    - `grep -n "class GState" custom_components/necromancer/core/state.py`
    - `grep -n "from .state import GState\|from .links import LinkCoordinator\|self.links = LinkCoordinator" custom_components/necromancer/core/engine.py`
  - **Assert:** Alle drei greps liefern Treffer (jeweils ≥1). core/engine.py-grep zeigt alle drei Marker-Zeilen.
  - **Cleanup:** —

- [ ] **M1.2 — Kein Zugriff auf Partner-Privates; Peers über public `peer.links`** · `P0`
  - **Prüft:** Engines fassen keine fremden Privatfelder mehr an — der alte `partner._following` / `partner._on_partner_repair_*`-Zugriff ist weg; Peers werden über `partner.links.*` (public) erreicht.
  - **Files:** `core/links.py` → `find_repairing_partner` nutzt `partner.links.following`, `notify_start/done` rufen `partner.links.on_partner_repair_start/done`. core/engine.py-Delegatoren verweisen auf `self.links.*`.
  - **Treiber:**
    - `grep -rn "partner\._following\|partner\._on_partner_repair" custom_components/necromancer/core/links.py custom_components/necromancer/core/engine.py` → MUSS leer sein.
    - `grep -n "partner.links.\|\.links\.on_partner_repair\|\.links\.following" custom_components/necromancer/core/links.py`
  - **Assert:** Erster grep liefert KEINE Treffer (kein `partner._following`/`partner._on_partner_repair` mehr — Peers nur über `partner.links.*`). Der einzige verbleibende `_following`-Bezug in core/engine.py ist `self._following` (eigene Property im `_evaluate`) bzw. die Delegator-Property selbst (als `self.links.following`) — kein Fremdzugriff. Zweiter grep zeigt die public `partner.links.*`-Aufrufe.
  - **Cleanup:** —

- [ ] **M1.3 — Alle vier Suiten grün (91) = Verhalten unverändert** · `P0`
  - **Prüft:** Die Extraktion ist verhaltenserhaltend — die vollständige In-Process-Suite bleibt grün.
  - **Treiber:** `cd <ha-core> && for t in units poe engine integration; do PYTHONPATH=<ha-core>:<ha-core>/config python tests/test_$t.py 2>&1 | tail -1; done`
  - **Assert:** Genau diese vier Schlusszeilen: `29 passed, 0 failed` · `16 passed, 0 failed` · `34 passed, 0 failed` · `12/12 checks passed` → Summe 91. Kein `failed`/`FAIL`.
  - **Cleanup:** —

- [ ] **M1.4 — Live-Smoke: Linking-Verhalten nach Extraktion unverändert** · `P2`
  - **Prüft:** Der verlinkte Happy-Path verhält sich live identisch zu vor der Extraktion (Leader→cooldown, Follower folgt ohne Eigen-Cycle).
  - **Treiber:** B2.4 ausführen (siehe oben).
  - **Assert:** Identisch zu B2.4: beide Guards `cooldown`/`ok`, Follower 0 eigene Versuche, Log-Marker `following (hold, verify after)` + `healthy after linked-guard repair`.
  - **Cleanup:** wie B2.4

---

## Guard-Linking · PoE-Fabric · Pitfalls F1–F6/CC7 · Automatisierung

### P0 — Guard-Linking (LinkCoordinator)

> **Rolle = Erstellungsreihenfolge** (s. Voraussetzungen → Konventionen): Asserts rollen-agnostisch über beide Guards.

- [ ] **LINK-1 — Link-Checkboxen symmetrisch (Add + Reconfigure)** · `P1`
  - **Prüft:** Die Link-Auswahl wird beidseitig wirksam — eine einseitige Deklaration verhält sich zur Laufzeit wie eine volle Gruppe (Clique-Schließung).
  - **Files:** `core/links.py` → `link_components`/`group_of` (ungerichteter Union + Connected-Components, stale ids werden verworfen); `config_flow.py` → `group_of(...)` liefert die Section-Defaults; `config_flow_helpers/schemas.py` → `_link_section` baut den `SECTION_LINK` (schemas.py) / `CONF_LINKED_GUARDS`-Selector (schemas.py), zeigt nur recover-fähige andere Guards.
  - **Treiber:** Zwei Recover-Guards auf `input_boolean.test_5` anlegen, beim **zweiten** den ersten als `linked_guards` setzen: `s1=N.create_guard({...,"name":"LinkA",...})`; `s2=N.create_guard({...,"name":"LinkB","linked_guards":[s1[1]],...})`. Danach `N.list_subentries(N.hub_id())` lesen.
  - **Assert:** Beide Subentries existieren; im Reconfigure-Flow von **LinkA** ist LinkB als Partner vorausgewählt (symmetrisch via `group_of`), obwohl nur bei LinkB deklariert.
  - **Cleanup:** `N.delete_subentry(*s2); N.delete_subentry(*s1)`

- [ ] **LINK-2 — Follower folgt, löst nicht selbst aus, verifiziert eigene Health** · `P0`
  - **Prüft:** Geht ein Gruppen-Partner in RECOVERING, *folgt* der andere (hold + danach Re-Verify gegen eigene Health) statt einen konkurrierenden Cycle zu starten → kein Doppel-Port-Cycle.
  - **Files:** `core/links.py` → `LinkCoordinator.on_partner_repair_start` (setzt `following=True`, `_set_state(RECOVERING)`); `core/links.py` → `validate_after_repair` (healthy → `_recover_success`); `core/engine.py` → `_evaluate` (`if self._following: emit; return`) und `core/engine.py` `_run_recovery_cycle` finally → `links.notify_done`.
  - **Treiber:** LinkA+LinkB wie LINK-1 auf `input_boolean.test_5`, beide `action_check` mit Heil-Aktion `input_boolean.turn_on test_5`. Health brechen: `N.call("input_boolean","turn_off",entity_id="input_boolean.test_5")`. `N.wait(debounce+boot_window+2)`. Dann `N.log()`.
  - **Assert:** (rollen-agnostisch, s. Konventionen) Über **beide** Guards: genau einmal `"linked guard is repairing — following (hold, verify after)"` **und** `"healthy after linked-guard repair"` (beim Follower); beide `N.guard(...)` → `cooldown`/`ok`, je `recover_count=1`; **genau eine** `"recovery attempt 1/"`-Zeile insgesamt (nur der Leader cyclet — kein Doppel-Cycle).
  - **Cleanup:** `N.delete_subentry(*s2); N.delete_subentry(*s1)`

- [ ] **LINK-3 — Synchroner RECOVERING-Claim, Partner konkurrieren nie** · `P0`
  - **Prüft:** Brechen beide gleichzeitig durch den Debounce, beansprucht einer synchron die Leader-Rolle (`_set_state(RECOVERING)` vor dem Task), der zweite findet ihn via `find_repairing_partner` und folgt.
  - **Files:** `core/engine.py` → `_start_cycle` (synchroner `_set_state(GState.RECOVERING)` vor `async_create_task`); `core/engine.py` → `_debounce_done` (`if (leader := self._find_repairing_partner())`); `core/links.py` → `find_repairing_partner` (Partner in RECOVERING/VERIFY und **nicht** `following`, erreicht über `partner.links.following`).
  - **Treiber:** LinkA+LinkB mit **gleichem** kleinen `debounce` auf `input_boolean.test_5`. Health brechen, `N.wait(debounce+2)`, dann `N.log()`.
  - **Assert:** Genau ein Guard zeigt `"debounce elapsed, starting recovery"`; der andere `"already repairing — following instead"` (engine) oder `"linked guard is repairing — following (hold, verify after)"` (links). Nur **ein** Recovery-Driver-Aufruf insgesamt.
  - **Cleanup:** `N.delete_subentry(*s2); N.delete_subentry(*s1)`

- [ ] **LINK-4 — Follower-Erfolg → COOLDOWN + Event; Follower-Erfolg-Notify standardmäßig still** · `P1`
  - **Prüft:** Ein erfolgreich „mitgeheilter" Follower durchläuft denselben Erfolgspfad (COOLDOWN, `recover_count++`) und feuert weiter das `necromancer_guard_repair`-Event — aber **kein** `recovery_success`-Notify (Push), außer der Guard hat `behavior.notify_follower_success` an (Checkbox in der Verknüpfte-Guards-Section). Misserfolg (`linked_repair_failed`) meldet immer.
  - **Files:** `core/links.py` → `validate_after_repair` (healthy → `_recover_success(via_link=True)`); `core/engine.py` → `_recover_success(via_link)` (Notify nur wenn `not via_link or behavior.notify_follower_success`); `config_flow_helpers/schemas.py` → `_link_section` (BooleanSelector `notify_follower_success`), `_build_data` (speichert Flag in behavior); `core/links.py` → `notify_start`/`notify_done` (`EVENT_GUARD_REPAIR`).
  - **Treiber:** LINK-2-Setup (Default: Flag aus). Variante B: Follower mit `notify_follower_success=true` rekonfigurieren.
  - **Assert:** Default: beide Status-Sensoren `cooldown`→`ok`, beide `recover_count=1`, `necromancer_guard_repair` pro Guard gefeuert, aber Follower-`recovery_success`-Notify **fehlt** (nur Leader meldet). Variante B: Follower meldet auch `recovery_success`. Automatisiert: `test_engine.py::test_follower_success_notify_gated`.
  - **Cleanup:** `N.delete_subentry(*s2); N.delete_subentry(*s1)`

- [ ] **LINK-5 — Leader scheitert + Follower noch krank → Follower eskaliert (kein Kaskaden-Recovery)** · `P1`
  - **Prüft:** Heilt der Leader die geteilte Ursache nicht und der Follower ist weiter unhealthy, folgt der Follower der Eskalation statt eine konkurrierende (die Gruppe re-triggernde) Recovery zu starten.
  - **Files:** `core/links.py` → `validate_after_repair` (still unhealthy + `leader_success=False` → `_set_state(ESCALATED)` + `_notify("linked_repair_failed")`).
  - **Treiber:** LinkA+LinkB, beide Aktion schreibt nur `input_text.test_note` (heilt NICHT). Health brechen, `N.wait(debounce+boot_window*max_attempts+2)`, `N.log()`.
  - **Assert:** Follower-Log `"linked repair failed and still unhealthy — escalating"`; `N.guard("linkb")` → `escalated`, `recover_count=0`, Follower-Driver-`calls=0` (nie eigene Recovery).
  - **Cleanup:** `N.call("input_boolean","turn_on",entity_id="input_boolean.test_5"); N.delete_subentry(*s2); N.delete_subentry(*s1)`

- [ ] **LINK-6 — CC7: Auto-aus → Follower folgt NICHT, eskaliert lokal** · `P0`
  - **Prüft:** Ein Guard mit deaktivierter Auto-Reparatur nimmt nie an einer Gruppen-Reparatur teil; ist sein eigenes Gerät betroffen, eskaliert er (Alarm) statt still zu folgen.
  - **Files:** `core/links.py` → `on_partner_repair_start` (`if not eng.auto: if health==UNHEALTHY and state!=ESCALATED → WARNING + _set_state(ESCALATED) + _notify("no_auto_recovery", reason="auto_off")`).
  - **Treiber:** LinkA+LinkB linked. Bei einem (hier LinkB) Auto-Reparatur ausschalten **und verifizieren**: `N.call("switch","turn_off",entity_id="switch.linkb_auto_reparatur")`; prüfe `N.st("switch.linkb_auto_reparatur")["state"] == "off"` (sonst läuft der Guard mit Auto-an → Szenario ungültig). Dann Health brechen, `N.wait(debounce+2)`, `N.log()`.
  - **Assert:** `N.guard("linkb")` → `escalated`; im Log für LinkB `"auto-recovery is off"` (als Follower `"linked guard repairing but auto-recovery is off — escalating"`, oder — falls LinkB selbst zuerst auslöst — `"still unhealthy but auto-recovery is off"`); **kein** `"recovery attempt 1/"` für LinkB.
  - **Cleanup:** `N.call("switch","turn_on",entity_id="switch.linkb_auto_reparatur"); N.delete_subentry(*s2); N.delete_subentry(*s1)`

- [ ] **LINK-7 — Auflösen: Abwahl trennt beidseitig, Clique-Schließung** · `P1`
  - **Prüft:** Einen Partner abwählen entfernt die Kante in beiden Richtungen; transitive Gruppen (A-B, B-C) bleiben zusammen, bis jemand alle Kanten desselben Linktyps löst.
  - **Files:** `core/links.py` → `link_components` (Connected-Components), `core/links.py` → `group_of` (Gruppe ohne sich selbst); `config_flow.py` → Reconfigure schreibt den `linked_guards`-Diff beidseitig in die Partner-Subentries zurück.
  - **Treiber:** A,B,C anlegen, A↔B und B↔C linken (beim Anlegen B→A, C→B deklarieren). Im Reconfigure von B den Partner A abwählen, speichern. **`linked_guards` aus dem Storage lesen** (`config/.storage/core.config_entries` → Entry → `subentries[].data.linked_guards`) — `N.list_subentries` (WS) liefert NUR Metadaten (`subentry_id`/`title`/`subentry_type`/`unique_id`), KEIN `data`.
  - **Assert:** Nach der Abwahl trägt weder A noch B die Kante A–B (Reconfigure schreibt den Diff beidseitig, `config_flow.py`); B↔C bleibt. **Hinweis zur Speicherform:** Beim *Anlegen* wird `linked_guards` **gerichtet** abgelegt (nur im deklarierenden Guard, z. B. B:[A], C:[B] — A bleibt []); die *ungerichtete* Gruppe berechnet `link_components` zur **Laufzeit**. Daher NICHT auf symmetrische Speicherung nach dem Create prüfen — die Symmetrie ist Laufzeit-Verhalten (durch LINK-2 belegt: B folgt A, obwohl A B nicht speichert). Der beidseitige Diff-Writeback gilt für den **Reconfigure**-Pfad.
  - **Cleanup:** alle drei `N.delete_subentry(...)`

- [ ] **LINK-8 — Teardown race-safe: Stop eskaliert Follower nicht** · `P1`
  - **Prüft:** Wird der Leader während eines laufenden Cycles gestoppt/entladen, meldet die abgebrochene Cycle-`finally` **keinen** (gescheiterten) Repair an die Gruppe — Follower bleiben hängend, eskalieren nicht.
  - **Files:** `core/engine.py` → `async_stop` (`_stopping=True`, `links.reset()`, `_cycle_task.cancel()`); `core/engine.py` → `_run_recovery_cycle` finally (`if not self._stopping: links.notify_done(...)`); `core/links.py` → `on_partner_repair_done`/`validate_after_repair` (`if eng._busy() or eng._stopping: return`, `_cycle_task=None` im finally).
  - **Treiber:** Primär durch Engine-Unit-Test gespiegelt — Live über Reload während eines blockierenden `recover()` schwer reproduzierbar. Live-Smoke: LinkA+LinkB, Health brechen, sofort `POST /api/config/config_entries/entry/<hub>/reload`, danach `N.log()`.
  - **Assert:** `N.log()` zeigt für den Follower **kein** `"escalating"` aus der Teardown-Phase; `N.g("/api/config")["state"]=="RUNNING"`, 0 Tracebacks. (Abgedeckt durch `test_engine.py::test_leader_stop_does_not_escalate_follower`.)
  - **Cleanup:** `N.delete_subentry(*s2); N.delete_subentry(*s1)`

### P0 — PoE-Fabric = einzige PoE-Autorität (H1b)

- [ ] **POE-1 — `resolve_with_reason`: 1 / 0 / >1 Match** · `P0`
  - **Prüft:** Genau ein Live-Match → Port (+ Cache-Refresh); 0 Live → last-known Cache; mehrdeutig (>1) → verweigert mit Grund; jeder gemeldete Port-id-Wert per DEBUG auditierbar.
  - **Files:** `core/poe.py` → `resolve_with_reason` (`len(live)==1` → `_learn`+return; `>1` → `f"'{identifier}' matches {len(live)} ports"`; cache-Fallback; sonst `f"no port matches '{identifier}'"`); DEBUG-Trace `core/poe.py` `"PoE %s:   port %r reports id %r"`.
  - **Treiber:** Abgedeckt durch `test_poe.py::test_resolve_live_single`, `test_resolve_ambiguous`, `test_resolve_last_known`, `test_resolve_none`. Live-Smoke: Port mit `id_entity` anlegen, `N.setstate(id_entity,"aa:bb")`, einen `poe_port`-Guard mit `expected_id="aa:bb"` → `N.guard(...)` `target`-Attribut prüfen.
  - **Assert:** Status-Sensor-`target` nennt das gelöste Port-Label; bei Mehrdeutigkeit ERROR-Log `"matches 2 ports"` und `can_recover` blockt (Guard → escalated).
  - **Cleanup:** `N.remove_port("<label>"); N.delete_subentry(...)`

- [ ] **POE-2 — Stale-Cache-Drop bei umgekabeltem Port (B1)** · `P0`
  - **Prüft:** Liegt der last-known Port jetzt auf einer **anderen** Live-id, wird der gecachte Eintrag verworfen und die Auflösung verweigert (kein Reboot des falschen Geräts).
  - **Files:** `core/poe.py` → `resolve_with_reason` (Cache-Fallback: `occupant = _norm(self._port_id(port))`; `occupant is None` → last-known WARNING + return; sonst `"now serves %r — dropping stale cache"` + `_cache.pop(target, None)` + refuse).
  - **Treiber:** Abgedeckt durch `test_poe.py::test_resolve_last_known_skips_occupied_port` (cache `{"aa:aa":"P1"}`, P1 meldet jetzt fremde id → `f.cache.get("aa:aa") is None`).
  - **Assert:** `test_resolve_last_known_skips_occupied_port` grün; im Log `"dropping stale cache"`.
  - **Cleanup:** —

- [ ] **POE-3 — Coalescing statt Per-Port-Lock: Driver + Service teilen EINEN Cycle** · `P0`
  - **Prüft:** Mehrere gleichzeitige Aufrufer für **denselben** Port (poe_port-Driver **und** `necromancer.repair_poe_port`) laufen in genau **einen** Power-Cycle zusammen (`asyncio.shield` auf den In-Flight-Task) — kein Doppel-Cycle.
  - **Files:** `core/poe.py` → `repair` (`self._inflight[label]`; bei laufendem Task `"already recovering — joining in-flight cycle"` + `await asyncio.shield(task)`; sonst Task synchron registrieren), `_run_cycle`/`_cycle`. **Kein** `asyncio.Lock` mehr.
  - **Treiber:** Abgedeckt durch `test_poe.py::test_concurrent_callers_coalesce` und `test_driver_and_service_coalesce` (verbreitertes Cycle-Fenster über Stubs, zwei parallele Aufrufer → ein Cycle). Live: `N.call("necromancer","repair_poe_port",id="aa:bb")` während ein poe_port-Guard cyclet.
  - **Assert:** Beide Tests grün; Log `"already recovering — joining in-flight cycle"`; der Actuator wird genau einmal off/on geschaltet.
  - **Cleanup:** —
  - *Hinweis: ersetzt die OBSOLETEN Test-Namen `test_per_port_lock_serialises`/`test_driver_and_service_share_lock` (gelöscht).*

- [ ] **POE-4 — Service `repair_poe_port` heilt eigenständig + Status-Event** · `P1`
  - **Prüft:** `necromancer.repair_poe_port(id)` löst auf, cyclet und feuert `necromancer_poe_port` (good/recovering/failed) pro Port.
  - **Files:** `core/poe.py` → `repair`; `core/poe.py` → `_run_cycle` (`_set_status` PORT_RECOVERING→good/failed); `core/poe.py` → `_set_status` feuert `EVENT_PORT_STATUS = f"{DOMAIN}_poe_port"` (**definiert in `core/poe.py`, nicht const.py**); Service-Registrierung in `__init__.py` (has_service-Guard, `_repair_poe_port`, `async_register`).
  - **Treiber:** Port via `N.add_port({...})`, dann `N.call("necromancer","repair_poe_port",id="<expected_id>")`. `N.log()`.
  - **Assert:** Log `"PoE port"` mit Statuswechsel; Actuator-Entität wurde off/on geschaltet; Service-Status 200.
  - **Cleanup:** `N.remove_port("<label>")`

- [ ] **POE-5 — `poe_port`-Driver = dünner Fabric-Adapter (kein Per-Guard-Cache)** · `P1`
  - **Prüft:** Der `poe_port`-Driver delegiert resolve+cycle vollständig an `hass.data[DOMAIN]["fabric"]`; er hält keinen eigenen Cache.
  - **Files:** `core/drivers/poe_port.py` → `_fabric()` (`hass.data.get(DOMAIN, {}).get("fabric")`), `can_recover`→`fabric.resolve_with_reason`, `recover`→`fabric.repair`, `config_errors`→`fabric.port_count==0`.
  - **Treiber:** Abgedeckt durch `test_poe.py::test_driver_recover_cycles_via_fabric`, `test_driver_can_recover_and_target`, `test_driver_blocks_on_no_match`, `test_driver_no_ports_config_error`.
  - **Assert:** Diese vier Tests grün; ein `poe_port`-Guard ohne konfigurierte Ports → ERROR `"no ports configured"` und `escalated`.
  - **Cleanup:** —

- [ ] **POE-6 — Platzhalter-ids werden nie gelernt (kein „moved port"-Sturm)** · `P0`
  - **Prüft:** `-`/leer/`unknown`/`unavailable`/`none` → `_norm`=None → nie gelernt, matchen nie einen Guard → keine fälschlichen Re-Cabling-WARNINGs.
  - **Files:** `core/poe.py` → `_PLACEHOLDER_IDS`, `core/poe.py` → `_norm` (collapse to None), `core/poe.py` → `_relearn` (nur bei `pid` truthy → `_learn`), `resolve_with_reason` (`target=_norm(identifier)`).
  - **Treiber:** Abgedeckt durch `test_poe.py::test_placeholder_ids_are_never_learned`. Live: Port-id-Entity auf `"-"` setzen, `POST .../entry/<hub>/reload`, `N.log()`.
  - **Assert:** `test_placeholder_ids_are_never_learned` grün; **0** `"moved port"`-WARNINGs nach Reload.
  - **Cleanup:** —

- [ ] **POE-7 — Re-Cabling-WARNING nur bei echtem Wechsel einer realen id** · `P2`
  - **Prüft:** Wandert eine **reale** id Port A→B, folgt der Cache und es feuert **eine** WARNING `"moved port"`; Platzhalter lösen nie eine aus.
  - **Files:** `core/poe.py` → `_learn` (`prev is None` → INFO `"learned"`; sonst WARNING `"%r moved port %r -> %r"`).
  - **Treiber:** Abgedeckt durch `test_poe.py::test_relearn_recable_updates_cache`. Live schwer provozierbar (echte MAC umstecken) → nur Smoke.
  - **Assert:** `test_relearn_recable_updates_cache` grün; bei echtem Umstecken genau eine `"moved port"`-WARNING.
  - **Cleanup:** —

### P0 — Pitfall-Fixes F1–F6 + CC7

- [ ] **F1 — Doppelter Guard-Name beim Submit abgelehnt** · `P0`
  - **Prüft:** Ein bereits vergebener Guard-Name wird beim Submit geblockt (Fehler `duplicate_name`), nicht nur als Warnung.
  - **Files:** `config_flow.py` → `_name_taken`; `config_flow.py` → `elif self._name_taken(...): errors[CONF_NAME]="duplicate_name"`.
  - **Treiber:** Guard `DupX` anlegen, dann zweiten Flow mit demselben `name="DupX"` treiben; die letzte `_post_flow`-Antwort prüfen.
  - **Assert:** Antwort enthält `errors == {"name": "duplicate_name"}`, **kein** `create_entry`; `list_subentries` zählt nur **einen** `DupX`.
  - **Cleanup:** `N.delete_subentry(<entry>, <sub des ersten DupX>)`

- [ ] **F2 — Template-Health referenziert eigene Entity → Feedback-Loop-WARNING** · `P0`
  - **Prüft:** Eine (Template-)Health, die eine guard-eigene Entity referenziert, erzeugt eine WARNING (kein Crash). Feuert erst nach Reload/Neustart (Entities sind beim Erst-Load noch nicht registriert).
  - **Files:** `core/engine.py` → `_check_config` (`own = {e.entity_id ... e.platform==DOMAIN and e.unique_id.startswith(self._subentry_id)}`; `loop = own ∩ health.referenced_entities()` → WARNING `"references its own entit(ies) ... feedback loop"`).
  - **Treiber:** `template_based`-Guard anlegen, dessen Template `sensor.<slug>_status` o. ä. referenziert; danach `POST /api/config/config_entries/entry/<hub>/reload`; `N.log()`.
  - **Assert:** Log enthält `"feedback loop"` (gleiche Zeile nennt auch `"references its own entit"`); `N.g("/api/config")["state"]=="RUNNING"`, 0 Tracebacks. (Abgedeckt durch `test_integration.py::test_health_self_reference_warns`, das auf `"feedback loop" in cap.text()` prüft.)
  - **Cleanup:** `N.delete_subentry(...)`

- [ ] **F4 — Reason-Konstanten englisch & konsistent** · `P2`
  - **Prüft:** Recovery-Reason-Strings sind einheitlich englische Konstanten.
  - **Files:** `const.py` → `REASON_OBSERVE = "observe"` / `REASON_AUTO_OFF = "auto_off"`; `core/policies/base.py` gibt `REASON_AUTO_OFF` zurück, `core/policies/notify.py` `REASON_OBSERVE`; `core/engine.py` (`_debounce_done`) nutzt `REASON_OBSERVE`.
  - **Treiber:** — (rein statisch)
  - **Assert:** `grep` in `const.py` zeigt `REASON_AUTO_OFF` und `REASON_OBSERVE`; keine deutschen Reason-Strings im Code.
  - **Cleanup:** —

- [ ] **F6 — Leere Aktion(en) beim Submit abgelehnt** · `P0`
  - **Prüft:** `action`/`off_action`/`on_action` ohne Inhalt werden beim Submit geblockt (`action_required`).
  - **Files:** `config_flow.py` → `errors[CONF_ACTION]="action_required"`; `config_flow.py` → `off_action`/`on_action`.
  - **Treiber:** `action`-Strategie-Guard treiben, im Action-Step leere Aktion `[]` posten; letzte `_post_flow`-Antwort prüfen.
  - **Assert:** Antwort enthält `errors` mit `action_required`, **kein** `create_entry`.
  - **Cleanup:** —

- [ ] **CC7 — „bei aus bleibt aus": deaktivierte Auto-Reparatur eskaliert, handelt nie** · `P0`
  - **Prüft:** Auto-Reparatur aus → Guard eskaliert beim Health-Bruch, startet **keine** Recovery und folgt auch **keiner** Gruppen-Reparatur (s. LINK-6).
  - **Files:** `core/engine.py` → `_debounce_done` (`policy.should_attempt(auto_enabled=self.auto)` → not allowed → `_notify("no_auto_recovery", reason)` + `_set_state(ESCALATED)`); `core/policies/base.py` liefert `REASON_AUTO_OFF`; `core/links.py` → `on_partner_repair_start` (`if not eng.auto: ... ESCALATED`).
  - **Treiber:** Recover-Guard `CC7x` auf `input_boolean.test_5`, `N.call("switch","turn_off",entity_id="switch.cc7x_auto_reparatur")`. Health brechen, `N.wait(debounce+2)`.
  - **Assert:** `N.guard("cc7x")` → `escalated`, `recover_count=0`, Driver nie aufgerufen; Notify-Key `no_auto_recovery` (de: „Problem erkannt, Auto-Reparatur ist deaktiviert."). Im Log `"auto-recovery is off"`.
  - **Cleanup:** `N.call("input_boolean","turn_on",entity_id="input_boolean.test_5"); N.delete_subentry(...)`

### Automatisiert statt manuell

- [ ] **AUTO-1 — Automatisierte Suiten laufen grün (29/16/34/7)** · `P0`
  - **Prüft:** Die vier Real-HA-core-Suiten (`tests.common.async_test_home_assistant`) sind grün und decken PoE resolve/cycle/coalescing/Platzhalter, Engine-State-Machine + Persistenz, Health-Registry-Events inkl. Template-Blind-Erkennung (B3), Linking-Koordination ab.
  - **Files:** `tests/test_units.py` (29), `test_poe.py` (16), `test_engine.py` (34), `test_integration.py` (7 Test-Funktionen / 12 `ok(...)`-Checks). Health-Tests u. a. `test_health_self_reference_warns`, `test_health_template_all_missing_is_blind`, `test_health_template_partial_missing_warns_only`. Linking-Tests u. a. `test_engine.py::test_linked_follower_recovers_with_leader`, `test_linked_follower_escalates_when_leader_fails`, `test_linked_auto_off_follower_escalates`, `test_leader_stop_does_not_escalate_follower`, `test_debounce_arbitration_second_follows`.
  - **Treiber:** Aus `<ha-core>`: `PYTHONPATH=<ha-core>:<ha-core>/config python -m pytest tests -q` (in-process, kein laufender Server nötig).
  - **Assert:** `test_units` 29, `test_poe` 16, `test_engine` 34 passed; `test_integration` grün (7 Test-Funktionen → `12/12 checks passed`). Gesamt **kein** FAIL/ERROR.
  - **Cleanup:** —
  - *Hinweis: Doc-Zähler 18/16/30/„7" sind STALE → korrigiert auf 29/16/34/7.*

- [ ] **AUTO-2 — Gates grün (ruff/format)** · `P1`
  - **Prüft:** Lint-/Format-Gates bestehen für das Necromancer-Paket.
  - **Treiber:** Aus `<ha-core>`: `uv run ruff check custom_components/necromancer` und `uv run ruff format --check custom_components/necromancer` (ruff findet die repo-eigene `pyproject.toml` über den Ziel-Pfad — **nie** aus `repo/` ausführen).
  - **Assert:** `ruff check` „All checks passed!"; `ruff format --check` ohne Änderungsvorschlag.
  - **Cleanup:** —

---

## Health-Quellen (state/template) · 4 Strategien + Health-Check-Toggle

### Health-Quellen: state_based vs template_based

- [ ] **HQ1 — Source-Step zeigt Radio state/template** · `P0`
  - **Prüft:** Der erste Schritt der Gerät-hinzufügen-Subentry bietet die Zustandsquelle als List-Radio `state_based`/`template_based`.
  - **Files:** `config_flow_helpers/schemas.py` → `_source_schema` (`options=[SOURCE_STATE, SOURCE_TEMPLATE]`, `translation_key="source_type"`) · `const.py` (`SOURCE_STATE="state_based"`, `SOURCE_TEMPLATE="template_based"`).
  - **Treiber:** Flow direkt starten (POST-only): `import requests; r=requests.post(N.BASE+"/api/config/config_entries/subentries/flow", headers=N.H, json={"handler":[N.hub_id(),"device"]}, timeout=15).json(); fid=r["flow_id"]`.
  - **Assert:** `r["step_id"]=="user"` und im `data_schema` hat das Feld `source_type` ein `select`-Selector mit options `["state_based","template_based"]`.
  - **Cleanup:** — (Flow nie abgeschlossen).

- [ ] **HQ2 — state_based: Device-Step zeigt flache Health-Felder** · `P0`
  - **Prüft:** Bei `state_based` enthält der Device-Step die flachen Felder `entity_id` + `attribute` + on/off-Werte (Top-Level, KEINE Section), KEIN Template.
  - **Files:** `config_flow_helpers/schemas.py` → `_health_fields`, `_watch_fields`, `_device_schema`. (Es gibt KEIN `SECTION_STATE`/`state_check` mehr — die Felder sind flach.)
  - **Treiber:** Flow wie HQ1 starten (`fid=r["flow_id"]`), dann `r=requests.post(N.BASE+f"/api/config/config_entries/subentries/flow/{fid}", headers=N.H, json={"source_type":"state_based"}, timeout=15).json()`.
  - **Assert:** `r["step_id"]=="device"`; `data_schema` enthält auf Top-Level die Felder `name`,`entity_id`,`on_value`,`off_value`,`device_id` (kein `state_check`-Wrapper, kein `template`).
  - **Cleanup:** —

- [ ] **HQ3 — template_based: Device-Step zeigt flaches Template-Feld** · `P0`
  - **Prüft:** Bei `template_based` enthält der Device-Step das flache Feld `template` (TemplateSelector, Top-Level, KEINE Section), KEINE Entität/on-off.
  - **Files:** `config_flow_helpers/schemas.py` → `_health_fields` (Template-Zweig, `TemplateSelector()`), `_device_schema`. (Es gibt KEIN `SECTION_TEMPLATE`/`template_check` mehr.)
  - **Treiber:** Flow wie HQ1 starten (`fid=r["flow_id"]`), dann `r=requests.post(N.BASE+f"/api/config/config_entries/subentries/flow/{fid}", headers=N.H, json={"source_type":"template_based"}, timeout=15).json()`.
  - **Assert:** `r["step_id"]=="device"`; `data_schema` enthält auf Top-Level das Feld `template` (selector `template`); kein `state_check`-Wrapper, kein `entity_id`.
  - **Cleanup:** —

- [ ] **HQ4 — state_based end-to-end: Guard reagiert auf Health-Entity** · `P0`
  - **Prüft:** Ein angelegter state_based-Guard wird über `watched_entities` event-getrieben und wechselt bei Health=off in SUSPECT → nach debounce in Recovery.
  - **Files:** `core/health/entity_state.py` → `watched_entities`, `evaluate`.
  - **Treiber:** `N.setstate("input_boolean.test_1","on")`; `eid,sub=N.create_guard({"source_type":"state_based","name":"HQstate","health":{"entity_id":"input_boolean.test_1","on_value":["on"],"off_value":["off"]},"mode":"recover","strategy":"action","action":[{"service":"input_text.set_value","data":{"entity_id":"input_text.test_note","value":"hit"}}],"behavior":{"debounce":2,"cooldown":3}})`; `N.guard("hqstate")` → erwartet `ok`; `N.setstate("input_boolean.test_1","off"); N.wait(1)`.
  - **Assert:** `N.guard("hqstate")[0]=="suspect"` (innerhalb debounce); nach `N.wait(3)` enthält `N.log()` `"HQstate debounce elapsed, starting recovery"`.
  - **Cleanup:** `N.delete_subentry(eid,sub)`; `N.setstate("input_boolean.test_1","on")`.

- [ ] **HQ5 — template_based + Tracking re-evaluiert bei Referenz-Änderung** · `P0`
  - **Prüft:** Template `{{ is_state('input_boolean.test_2','on') }}` wird via `async_track_template_result` getrackt; Ändern der referenzierten Entity re-evaluiert Health. `watched_entities==[]`.
  - **Files:** `core/health/template.py` → `async_setup`, `watched_entities`, `evaluate`.
  - **Treiber:** `N.setstate("input_boolean.test_2","on")`; `eid,sub=N.create_guard({"source_type":"template_based","name":"HQtmpl","health":{"template":"{{ is_state('input_boolean.test_2','on') }}"},"mode":"recover","strategy":"action","action":[{"service":"input_text.set_value","data":{"entity_id":"input_text.test_note","value":"x"}}],"behavior":{"debounce":2,"cooldown":3}})`; `N.guard("hqtmpl")` → `ok`; `N.setstate("input_boolean.test_2","off"); N.wait(1)`.
  - **Assert:** `N.guard("hqtmpl")[0]=="suspect"` (Template re-evaluierte ohne eigenes watched_entities-Abo).
  - **Cleanup:** `N.delete_subentry(eid,sub)`; `N.setstate("input_boolean.test_2","on")`.

- [ ] **HQ-CHK — template_based + Health-Check: VERIFY greift, recovered** · `P0`
  - **Prüft:** Ein Template-Guard mit `*_check`-Strategie geht nach `recover()` in VERIFY; heilt die Aktion die Template-Bedingung, re-evaluiert das Template → Health=OK → COOLDOWN (Template ist prüfbar, anders als ein Trigger).
  - **Files:** `core/health/template.py` → `evaluate` (on-demand für VERIFY); `core/engine.py` → `_run_recovery_cycle` (`_set_state(VERIFY)`→`_wait_health_ok`→`_recover_success`), `_wait_health_ok`.
  - **Treiber:** `N.setstate("input_boolean.test_5","off")`; `eid,sub=N.create_guard({"source_type":"template_based","name":"HQtcheck","health":{"template":"{{ is_state('input_boolean.test_5','on') }}"},"mode":"recover","strategy":"action_check","action":[{"service":"input_boolean.turn_on","data":{"entity_id":"input_boolean.test_5"}}],"behavior":{"debounce":1,"boot_window":10,"cooldown":3,"max_attempts":2}})`; `N.wait(4)`.
  - **Assert:** `N.log()` enthält `"HQtcheck recovered after 1 attempt(s)"`; `N.guard("hqtcheck")[0]` in `("cooldown","ok")`; `attrs["recover_count"]>=1`.
  - **Cleanup:** `N.delete_subentry(eid,sub)`; `N.setstate("input_boolean.test_5","off")`.

- [ ] **HQ6 — Template-Verdicts: kein Fehlalarm bei unklarem Ergebnis** · `P0`
  - **Prüft:** Whitelist: `{{ true }}`/`on`/`1`/`yes`→OK · `{{ false }}`/`off`/`0`/`no`→UNHEALTHY · alles andere (`{{ 'kaputt' }}`, `unavailable`, leer, `none`, `{{ states('sensor.does_not_exist') }}`, Render-Fehler)→UNKNOWN (kein SUSPECT, keine Recovery).
  - **Files:** `core/health/template.py` → `evaluate` (Whitelist: `_HEALTHY_RESULTS={"true","on","1","yes"}` → OK, `_FAULTY_RESULTS={"false","off","0","no"}` → UNHEALTHY, `TemplateError`/alles übrige → UNKNOWN). Es gibt KEIN `_UNKNOWN_RESULTS`-Set und KEIN `result_as_boolean` mehr.
  - **Treiber:** Drei kurzlebige notify-Guards anlegen (`mode="notify"`, `behavior={"debounce":2}`): a) `health.template="{{ true }}"`, b) `"{{ false }}"`, c) `"{{ states('sensor.does_not_exist') }}"`. Nach Anlegen je `N.wait(1)`.
  - **Assert:** a) `N.guard(slug)[0]=="ok"`; b) nach `N.wait(3)` Log `"<name> problem detected (notify-only)"`; c) bleibt `ok` (UNKNOWN ⇒ KEIN `"problem detected"` für c im Log).
  - **Cleanup:** alle drei `N.delete_subentry(eid,sub)`.

- [ ] **HQ-STATE-UNK — state_based: unavailable/unknown → UNKNOWN, kein Recover** · `P1`
  - **Prüft:** Eine state_based-Health, deren Entity `unavailable`/`unknown` meldet (und nicht explizit als `off_value` gelistet), liefert UNKNOWN statt UNHEALTHY → kein Fehlalarm, kein Recovery.
  - **Files:** `core/health/entity_state.py` → `evaluate` (`actual in (STATE_UNAVAILABLE, STATE_UNKNOWN) → UNKNOWN`) (expliziter `off_value` würde gewinnen).
  - **Treiber:** `N.setstate("input_boolean.test_3","on")`; `eid,sub=N.create_guard({"source_type":"state_based","name":"HQunk","health":{"entity_id":"input_boolean.test_3","on_value":["on"],"off_value":["off"]},"mode":"notify","behavior":{"debounce":2}})`; `N.guard("hqunk")` → `ok`; `N.setstate("input_boolean.test_3","unavailable"); N.wait(3)`.
  - **Assert:** `N.guard("hqunk")[0]=="ok"` (bleibt ok); `N.log()` enthält KEIN `"HQunk problem detected (notify-only)"`.
  - **Cleanup:** `N.delete_subentry(eid,sub)`; `N.setstate("input_boolean.test_3","on")`.

- [ ] **HQ7 — Kaputtes Jinja im Flow abgelehnt** · `P0`
  - **Prüft:** Ungültiges Template (`{{ 1 + }}`) wird vom TemplateSelector validiert → Flow-Error, kein Submit.
  - **Files:** `config_flow_helpers/schemas.py` → `_health_fields` (`selector.TemplateSelector()` validiert serverseitig).
  - **Treiber:** Flow starten (`fid=r["flow_id"]`), `{"source_type":"template_based"}`, dann device-Step FLACH posten mit `{"name":"HQbad","template":"{{ 1 + }}"}` (kein `mode`-Feld im Device-Step — die Strategie-Wahl inkl. `notify` kommt erst im nächsten Step).
  - **Assert:** Antwort hat `errors` (z. B. `{"template":...}` oder `base`) bzw. `type!="create_entry"` und bleibt `step_id=="device"`.
  - **Cleanup:** — (kein Subentry erzeugt).

- [ ] **HQ8 — F2: Template referenziert eigene Entity → Feedback-Loop-WARNING** · `P1`
  - **Prüft:** Health-Template, das auf den eigenen Status-Sensor zeigt, löst nach Reload eine WARNING aus (kein Crash, HA bleibt RUNNING). Hinweis: feuert NUR nach Reload, nicht beim Erst-Load.
  - **Files:** `core/engine.py` (`"%s: health references its own entit(ies) %s — feedback loop"`), `core/health/template.py` → `referenced_entities`.
  - **Treiber:** `eid,sub=N.create_guard({"source_type":"template_based","name":"HQloop","health":{"template":"{{ is_state('sensor.hqloop_status','ok') }}"},"mode":"notify","behavior":{"debounce":2}})`; Reload erzwingen: `N.call("homeassistant","reload_config_entry",entry_id=eid)`; `N.wait(3)`.
  - **Assert:** `N.log()` enthält `"references its own entit(ies)"` UND `"feedback loop"`; `N.g("/api/config")["state"]=="RUNNING"`.
  - **Cleanup:** `N.delete_subentry(eid,sub)`.

### Strategien (4) + Health-Check-Toggle

- [ ] **ST1 — Strategie-Radio zeigt genau 5 (notify + 4), kein Mode-Feld im Device-Step** · `P0`
  - **Prüft:** Das Mode-Feld (Auto-Reparatur/Nur-benachrichtigen) ist aus dem Device-Step **entfernt**; die Wahl liegt jetzt als erste Option (`notify`) im Strategie-Step, gefolgt von den 4 Recovery-Strategien. Der Health-Check ist ein Toggle (`health_check`, default an) in jeder Recovery-Behaviour-Section.
  - **Files:** `config_flow_helpers/schemas.py` → `_device_schema` (kein `CONF_MODE` mehr), `_strategy_schema` (`options=[MODE_NOTIFY, *_STRATEGIES]`), `_build_data` (`notify_only = strategy == MODE_NOTIFY`); `config_flow.py` → `async_step_strategy`-Dispatch (`MODE_NOTIFY: async_step_notify`).
  - **Treiber:** Flow starten (`fid=r["flow_id"]`) → `{"source_type":"state_based"}` → device-Step FLACH posten mit `{"name":"STseven","entity_id":"input_boolean.test_1","on_value":["on"],"off_value":["off"]}` (KEIN `mode`).
  - **Assert:** Device-Step-Schema hat **kein** `mode`-Feld; `r["step_id"]=="strategy"`; das `strategy`-select hat options `["notify","switch","action","actions","poe_port"]` (5 Einträge, genau diese Reihenfolge). `notify` wählen → `step_id=="notify"`.
  - **Cleanup:** — (Flow nicht abgeschlossen).

- [ ] **ST2 — switch: off→delay→on Power-Cycle** · `P0`
  - **Prüft:** Strategie `switch` baut `switch_cycle`-Driver (homeassistant.turn_off → off_on_delay → turn_on). Ohne Health-Check ⇒ sofort recover_success.
  - **Files:** `config_flow_helpers/schemas.py` → `_build_driver` (`switch_cycle`), `core/drivers/switch_cycle.py` → `recover`.
  - **Treiber:** `N.setstate("switch.test_template_switch","on")`; `eid,sub=N.create_guard({"source_type":"state_based","name":"STswitch","health":{"entity_id":"input_boolean.test_1","on_value":["on"],"off_value":["off"]},"mode":"recover","strategy":"switch","switch_entity":"switch.test_template_switch","off_on_delay":1,"behavior":{"debounce":1,"cooldown":3}})`; `N.setstate("input_boolean.test_1","off"); N.wait(4)`.
  - **Assert:** `N.log()` enthält `"STswitch recovery attempt 1/"` und `"STswitch recovered after 1 attempt(s)"`; danach `N.guard("stswitch")[0]` in `("cooldown","ok")`; `attrs["recover_count"]>=1`.
  - **Cleanup:** `N.delete_subentry(eid,sub)`; `N.setstate("input_boolean.test_1","on")`.

- [ ] **ST3 — action (ohne Check): eine Sequenz, fire-and-forget** · `P0`
  - **Prüft:** Strategie `action` baut `action_call`-Driver; ohne Health-Check führt EIN recover() sofort zu recover_success ohne VERIFY.
  - **Files:** `config_flow_helpers/schemas.py` → `_build_driver` (`action_call`), `core/engine.py` (`if not health_check: _recover_success()`).
  - **Treiber:** `eid,sub=N.create_guard({"source_type":"state_based","name":"STact","health":{"entity_id":"input_boolean.test_1","on_value":["on"],"off_value":["off"]},"mode":"recover","strategy":"action","action":[{"service":"input_text.set_value","data":{"entity_id":"input_text.test_note","value":"fired"}}],"behavior":{"debounce":1,"cooldown":3}})`; `N.setstate("input_boolean.test_1","off"); N.wait(3)`.
  - **Assert:** `N.st("input_text.test_note")["state"]=="fired"`; `N.log()` enthält `"STact recovered after 1 attempt(s)"` (kein VERIFY-Zustand, da Check aus).
  - **Cleanup:** `N.delete_subentry(eid,sub)`; `N.setstate("input_boolean.test_1","on")`.

- [ ] **ST-RAISE — action wirft (OHNE Check) → kein Falsch-Erfolg → ESCALATED** · `P0`
  - **Prüft:** Wenn die Recovery-Aktion zur Laufzeit wirft (z. B. fehlender Service), wird das als FEHLGESCHLAGENER Versuch gewertet (nie recover_success), retry bis `max_attempts`, dann ESCALATED — auch ohne Health-Check. `recover_count` bleibt 0.
  - **Files:** `core/engine.py` → `_run_recovery_cycle` (BLE001-Pfad: `LOGGER.exception("Recovery driver failed for %s")`, retry/`_escalate`), `_escalate` (`"could not be recovered after"`).
  - **Treiber:** `N.setstate("input_boolean.test_1","on")`; `eid,sub=N.create_guard({"source_type":"state_based","name":"STraise","health":{"entity_id":"input_boolean.test_1","on_value":["on"],"off_value":["off"]},"mode":"recover","strategy":"action","action":[{"service":"nonexistent.boom","data":{}}],"behavior":{"debounce":1,"cooldown":3}})`; `N.setstate("input_boolean.test_1","off"); N.wait(4)`.
  - **Assert:** `N.guard("straise")[0]=="escalated"`; `attrs.get("recover_count",0)==0`; `N.log()` enthält `"STraise could not be recovered after"` (Pfad über recover-raise ODER recovery_blocked — die Invariante ist „kein Erfolg, recover_count==0, terminal ESCALATED").
  - **Cleanup:** `N.delete_subentry(eid,sub)`; `N.setstate("input_boolean.test_1","on")`.

- [ ] **ST4 — actions: getrennte Aus-/Ein-Aktion + Delay** · `P0`
  - **Prüft:** Strategie `actions` baut `action_cycle`-Driver mit `off_action`/`on_action`/`off_on_delay`.
  - **Files:** `config_flow_helpers/schemas.py` → `_build_driver` (`action_cycle`), `core/drivers/action_cycle.py`.
  - **Treiber:** `eid,sub=N.create_guard({"source_type":"state_based","name":"STacts","health":{"entity_id":"input_boolean.test_1","on_value":["on"],"off_value":["off"]},"mode":"recover","strategy":"actions","off_action":[{"service":"input_text.set_value","data":{"entity_id":"input_text.test_note","value":"OFF"}}],"on_action":[{"service":"input_text.set_value","data":{"entity_id":"input_text.test_note","value":"ON"}}],"off_on_delay":1,"behavior":{"debounce":1,"cooldown":3}})`; `N.setstate("input_boolean.test_1","off"); N.wait(4)`.
  - **Assert:** `N.st("input_text.test_note")["state"]=="ON"` (on_action lief zuletzt); `N.log()` enthält `"STacts recovered after 1 attempt(s)"`.
  - **Cleanup:** `N.delete_subentry(eid,sub)`; `N.setstate("input_boolean.test_1","on")`.

- [ ] **ST5 — poe_port: Resolver findet Port per expected_id** · `P0`
  - **Prüft:** Strategie `poe_port` baut dünnen Adapter; Driver delegiert resolve+cycle an die Fabric, findet Port über `expected_id` in der flachen Liste.
  - **Files:** `config_flow_helpers/schemas.py` → `_build_driver` (`poe_port`,`expected_id`); `core/drivers/poe_port.py` → `can_recover` (`fabric.resolve_with_reason`), `target_info` `config_errors`.
  - **Treiber:** Port anlegen: `N.add_port({"label":"STport","actuator":"input_boolean.sim_poe_port","id_static":"st-mac-1","status_entity":"input_boolean.sim_device_power","status_on":["on"],"status_off":["off"],"off_on_delay":1})`; `N.setstate("input_boolean.sim_device_power","on")`; Guard: `eid,sub=N.create_guard({"source_type":"state_based","name":"STpoe","health":{"entity_id":"input_boolean.test_1","on_value":["on"],"off_value":["off"]},"mode":"recover","strategy":"poe_port","expected_id":"st-mac-1","behavior":{"debounce":1,"boot_window":5,"cooldown":3,"max_attempts":2}})`; `N.setstate("input_boolean.test_1","on")`.
  - **Assert:** Guard kommt sauber hoch: `N.guard("stpoe")[0]=="ok"`; `N.log()` enthält für STpoe KEIN `"no ports configured"`, KEIN `"no port matches 'st-mac-1'"` und KEIN `"matches"` (resolve fand `st-mac-1`).
  - **Cleanup:** `N.delete_subentry(eid,sub)`; `N.remove_port("STport")`.

- [ ] **ST6 — *_check: VERIFY wartet auf Health=OK (Heal-Trick → recovered)** · `P0`
  - **Prüft:** Mit Health-Check geht die Engine nach recover() in VERIFY und wartet bis boot_window auf Health=OK; heilt die Aktion die Health, folgt COOLDOWN.
  - **Files:** `core/engine.py` → `_run_recovery_cycle` (`_set_state(VERIFY)`, `_wait_health_ok`→`_recover_success`), `_wait_health_ok`.
  - **Treiber:** `N.setstate("input_boolean.test_5","off")`; `eid,sub=N.create_guard({"source_type":"state_based","name":"STcheck","health":{"entity_id":"input_boolean.test_5","on_value":["on"],"off_value":["off"]},"mode":"recover","strategy":"action_check","action":[{"service":"input_boolean.turn_on","data":{"entity_id":"input_boolean.test_5"}}],"behavior":{"debounce":1,"boot_window":10,"cooldown":3,"max_attempts":2}})`; `N.wait(4)`.
  - **Assert:** `N.log()` enthält `"STcheck recovery attempt 1/2"` und `"STcheck recovered after 1 attempt(s)"`; `N.guard("stcheck")[0]` in `("cooldown","ok")`; `attrs["recover_count"]>=1`.
  - **Cleanup:** `N.delete_subentry(eid,sub)`; `N.setstate("input_boolean.test_5","off")`.

- [ ] **ST7 — *_check: Aktion heilt nicht → max_attempts → ESCALATED** · `P0`
  - **Prüft:** Mit Health-Check, wenn die Aktion die Health NICHT heilt, läuft VERIFY ins Timeout, retry bis max_attempts, dann ESCALATED (terminaler ERROR, kein Traceback).
  - **Files:** `core/engine.py` (`attempt>=max → _escalate`), `_escalate` (`"could not be recovered after"`).
  - **Treiber:** `N.setstate("input_boolean.test_1","off")`; `eid,sub=N.create_guard({"source_type":"state_based","name":"STfail","health":{"entity_id":"input_boolean.test_1","on_value":["on"],"off_value":["off"]},"mode":"recover","strategy":"action_check","action":[{"service":"input_text.set_value","data":{"entity_id":"input_text.test_note","value":"noheal"}}],"behavior":{"debounce":1,"boot_window":3,"cooldown":3,"max_attempts":2}})`; `N.wait(12)`.
  - **Assert:** `N.guard("stfail")[0]=="escalated"`, `attrs["attempt"]==2`, `attrs.get("recover_count",0)==0`; `N.log()` enthält `"STfail could not be recovered after 2 attempt(s)"`.
  - **Cleanup:** `N.delete_subentry(eid,sub)`; `N.setstate("input_boolean.test_1","on")`.

- [ ] **ST8 — Behavior-Section: health_check-Toggle + boot_window/max_attempts immer sichtbar** · `P0`
  - **Prüft:** Jede Recovery-Strategie (switch/action/actions/poe) zeigt in der Behaviour-Section den `health_check`-Toggle (default an) plus `boot_window` + `max_attempts` — unabhängig vom Toggle-Zustand (HA-birth/will-Muster: editierbar trotz aus). `cooldown` steht VOR dem Toggle, damit klar ist, dass der Toggle nur boot_window/max_attempts steuert.
  - **Files:** `config_flow_helpers/schemas.py` → `_behavior_section` (kein `check`-Param mehr; `health_check`/`boot_window`/`max_attempts` immer enthalten), `_build_data` (`check = bool(step2.get(CONF_HEALTH_CHECK, True))`), `_poe_schema` ruft `_behavior_section(d)`.
  - **Treiber:** Flow bis zum Strategie-Submit treiben (state_based, mode recover); `strategy=action` wählen; das `action`-Form inspizieren. Zweiter Flow mit `strategy=poe_port`.
  - **Assert:** Die `behavior`-Section enthält `debounce`,`cooldown`,`health_check`,`boot_window`,`max_attempts` (genau diese Reihenfolge); `health_check` hat `default==true`. Auch die `poe_port`-Behaviour-Section enthält `health_check`.
  - **Cleanup:** — (Flows nicht abgeschlossen).

- [ ] **ST9 — Reconfigure-Vorauswahl der Strategie via _current_strategy** · `P1`
  - **Prüft:** Beim Reconfigure ist das Strategie-Radio mit der gespeicherten Strategie vorbelegt — abgeleitet aus dem driver-type (der Health-Check ist separat der Toggle, nicht Teil der Strategie).
  - **Files:** `config_flow_helpers/schemas.py` → `_current_strategy`, `config_flow.py` → `async_step_strategy` (default aus `_current_strategy(self._reconfig_data())`).
  - **Treiber:** Guard `STrc` als `actions_check` anlegen (`strategy="actions_check"`, off/on-action + `behavior` mit boot_window/max_attempts). Reconfigure-Flow starten: `r=requests.post(N.BASE+"/api/config/config_entries/subentries/flow", headers=N.H, json={"handler":[eid,"device"],"subentry_id":sub}, timeout=15).json(); fid=r["flow_id"]`; Source-Step mit `{"source_type":"state_based"}` quittieren; device-Step durchreichen bis `strategy`.
  - **Assert:** Das `strategy`-Feld im Reconfigure-Strategie-Step hat `default=="actions"` (Basis-Strategie; der `actions_check`-Spec legt via Testkit `health_check: true` an, aber die Strategie ist `actions`).
  - **Cleanup:** `N.delete_subentry(eid,sub)`.

- [ ] **ST10 — Vorbedingung fehlt: switch_cycle ohne Switch → recovery_blocked → ESCALATED** · `P1`
  - **Prüft:** `can_recover` des switch_cycle-Drivers blockt, wenn die Switch-Entity fehlt → `recovery_blocked` → ESCALATED (kein recover()).
  - **Files:** `core/drivers/switch_cycle.py` → `can_recover`; `core/engine.py` (`if not ok: _escalate("recovery_blocked")`).
  - **Treiber:** `eid,sub=N.create_guard({"source_type":"state_based","name":"STnoswitch","health":{"entity_id":"input_boolean.test_1","on_value":["on"],"off_value":["off"]},"mode":"recover","strategy":"switch","switch_entity":"switch.does_not_exist","off_on_delay":1,"behavior":{"debounce":1,"cooldown":3}})`; `N.setstate("input_boolean.test_1","off"); N.wait(3)`.
  - **Assert:** `N.guard("stnoswitch")[0]=="escalated"`; `N.log()` enthält `"STnoswitch recovery blocked:"`.
  - **Cleanup:** `N.delete_subentry(eid,sub)`; `N.setstate("input_boolean.test_1","on")`.

- [ ] **ST11 — action ohne Aktion im Flow abgelehnt (action_required)** · `P1`
  - **Prüft:** Ein `action`/`action_check`-Guard ohne Aktion wird beim Submit abgelehnt (kann nur eskalieren) — F6.
  - **Files:** `config_flow.py` → `async_step_action` (`if not flat.get(CONF_ACTION): errors[CONF_ACTION]="action_required"`); `async_step_actions` (off+on je `action_required`).
  - **Treiber:** Flow bis zum `action`-Step treiben (state_based, mode recover, strategy `action`; `fid=r["flow_id"]`), dann posten mit leerer Aktion: `{"action":[], "behavior":{"debounce":1,"cooldown":3}, "notification":{}, "linked_guards":{}}`.
  - **Assert:** Antwort hat `errors["action"]=="action_required"`, `type!="create_entry"`, bleibt `step_id=="action"`.
  - **Cleanup:** — (kein Subentry).

- [ ] **RL1 — Reload-Geräte-Integration: Checkbox nur bei zugewiesenem Gerät + Reload nach Repair** · `P1`
  - **Prüft:** Die `reload`-Section (Checkbox „Integration neu laden" + Delay) erscheint in den Recover-Steps **nur wenn im Device-Step ein Gerät gesetzt** wurde; ist sie an, lädt die Engine nach `driver.recover()` (vor VERIFY) die Config-Entry des zugewiesenen Geräts neu (mit Delay).
  - **Files:** `config_flow_helpers/schemas.py` → `_reload_section` (SECTION_RELOAD, BooleanSelector + `_seconds_selector`), als `reload_block`-Parameter **vor** der Notification-Section in `_switch_schema`/`_action_schema`/`_actions_schema`/`_poe_schema` eingefügt; `_build_data` (speichert `behavior.reload_entry`/`reload_delay` nur bei gesetztem `device_id`); `config_flow.py` → `_reload_block()` (gibt die Section nur zurück, wenn `self._step1[CONF_DEVICE_ID]`), an die 4 Recover-Steps via `reload_block=`; `core/engine.py` → `_maybe_reload_device_entry` (nach `recover()`, vor VERIFY: `dr` → `device.primary_config_entry`/`config_entries` → `hass.config_entries.async_reload`, best-effort).
  - **Treiber:** Recover-Guard MIT zugewiesenem Gerät anlegen, Reload-Checkbox an, kleiner Delay; Health brechen, Repair abwarten.
  - **Assert:** Schema des Recover-Steps enthält die `reload`-Section nur bei gesetztem Gerät (ohne Gerät fehlt sie), und zwar **vor** der Notification-Section; nach dem Repair erscheint im Log `"reloading the assigned device's integration (entry …)"` und die Geräte-Integration wurde neu geladen. Automatisiert: `test_units.py::test_build_data_reload_entry`, `test_engine.py::test_reload_device_entry_on_repair`.
  - **Cleanup:** Subentry löschen.

---

## Auto-Reparatur · Notify-als-Aktion · Corner Cases/Robustheit

### P0 — Auto-Reparatur (Switch statt Config-Feld)

- [ ] **AR1 — Kein auto_restart-Feld im Wizard** · `P1`
  - **Prüft:** Die „Verhalten"-Section enthält KEIN `auto_restart`-Feld mehr; Auto wird nur über den Laufzeit-Switch gesteuert.
  - **Files:** `config_flow_helpers/schemas.py` → `_behavior_section` baut `debounce`/`cooldown`/`boot_window`/`max_attempts`, aber KEIN `CONF_AUTO_RESTART` (siehe Kommentar); `const.py` → `CONF_AUTO_RESTART` existiert nur für Persistenz/Default `DEFAULT_AUTO_RESTART=True`.
  - **Treiber:** `grep -n "auto_restart\|CONF_AUTO_RESTART" config_flow.py` → keine `vol.Optional/Required(CONF_AUTO_RESTART …)` in einem Step-/Section-Schema; Treffer nur im Kommentar.
  - **Assert:** Kein Schema-Feld `auto_restart` in `switch`/`action`/`actions`/`poe_port`/`notify`-Step.
  - **Cleanup:** —

- [ ] **AR2 — Switch-Default an, Toggle persistiert (überlebt Neustart)** · `P0`
  - **Prüft:** Guard startet mit `auto=True` (`DEFAULT_AUTO_RESTART`); `switch.<slug>_auto_reparatur` schreibt durch in den Store und überlebt Neustart.
  - **Files:** `switch.py` `async_turn_off` ruft `self._engine.set_auto(False)`; `core/engine.py` `set_auto` ruft `self._save()`; `core/engine.py` `_apply_persisted` restored `auto`; `core/engine.py` `snapshot()` schreibt `"auto"`; `__init__.py` Store-Key `f"{DOMAIN}.{entry.entry_id}"` (entry_id = Hub-Entry = `N.hub_id()`), je Subentry ein Eintrag mit `"auto"`.
  - **Treiber:** `N.create_guard({source_type:"state_based", name:"AutoPersist", health:{entity_id:"input_boolean.test_1", on_value:["on"], off_value:["off"]}, mode:"recover", strategy:"switch", switch_entity:"switch.test_template_switch", behavior:{debounce:5, cooldown:5}})` → `N.st("switch.autopersist_auto_reparatur")["state"]` == `"on"` → `N.call("switch","turn_off", entity_id="switch.autopersist_auto_reparatur")` → `N.wait(7)` (> `SAVE_DELAY=5`) → Store-Datei `<ha-core>/config/.storage/necromancer.<N.hub_id()>` lesen.
  - **Assert:** Vor Restart `N.st("switch.autopersist_auto_reparatur")["state"]=="off"`; in der Store-Datei steht unter `data.<subentry_id>` ein Objekt mit `"auto": false`. (Optional Restart-Variante per Runbook-Restart → Switch kommt als `off` hoch.)
  - **Cleanup:** `N.delete_subentry(entry, sub)`

- [ ] **AR3 — Auto aus → ESCALATED ohne Reparaturversuch + Notify `no_auto_recovery`** · `P0`
  - **Prüft:** Bei deaktivierter Auto-Reparatur eskaliert der Guard nach Debounce SOFORT (kein `recover()`), feuert `no_auto_recovery`-Notify, `recover_count` bleibt 0.
  - **Files:** `core/engine.py` `_debounce_done`: `policy.should_attempt(auto_enabled=self.auto)` → bei `not allowed` und `reason != REASON_OBSERVE` → `_notify("no_auto_recovery", reason=reason)` + `_set_state(GState.ESCALATED)`; `core/policies/base.py` liefert `REASON_AUTO_OFF`.
  - **Treiber:** Guard wie AR2 (Name `AutoOff`, `behavior:{debounce:5, cooldown:5}`). `N.call("switch","turn_off", entity_id="switch.autooff_auto_reparatur")` → `N.call("input_boolean","turn_off", entity_id="input_boolean.test_1")` → `N.wait(8)` → `N.guard("autooff")`.
  - **Assert:** `N.guard("autooff")[0] == "escalated"`; `attrs["recover_count"] == 0`; `N.log()` enthält `"still unhealthy but auto-recovery is off"`.
  - **Cleanup:** `N.delete_subentry(entry, sub)` + `N.call("input_boolean","turn_on", entity_id="input_boolean.test_1")`

### P0 — Notify als Aktion (ActionSelector + Variablen + de-Meldungen)

- [ ] **NA1 — ActionSelector in Section „Benachrichtigung" (optional)** · `P1`
  - **Prüft:** Notify ist eine optionale Aktions-Sequenz (ActionSelector) in der Section `notification`, KEINE Notify-Ziel-Liste.
  - **Files:** `config_flow_helpers/schemas.py` `_notification_section` → `vol.Optional(CONF_NOTIFY_ACTION …): selector.ActionSelector()`, Section-Name `SECTION_NOTIFY="notification"`; `const.py` `CONF_NOTIFY_ACTION="notify_action"`.
  - **Treiber:** `grep -n "ActionSelector" config_flow.py` (mind. die Notify-Section + Strategy-Action-Felder); kein `EntitySelector(domain="notify")` o. ä.
  - **Assert:** `_notification_section` baut genau ein optionales Feld `notify_action` als `ActionSelector`.
  - **Cleanup:** —

- [ ] **NA2 — `{{ message }}`/`{{ name }}`/`{{ event_text }}`/`{{ event }}` als Variablen verfügbar** · `P0`
  - **Prüft:** Die Notify-Aktion bekommt `message` (= „Name: Text"), `name`, `event_text` (Text OHNE Name), `event` (Notify-Key) + Event-Params (`attempt`, `max`, `attempts` [plural-korrekt], `reason`) als Variablen.
  - **Files:** `core/notify.py` → `_resolve(lang, name, key, params)` liefert `(message, event_text)` (message = `f"{name}: {event_text}"`, baut plural-`attempts`); `variables = {"message", "name", "event_text", "event", **params}` → `async_run(...)`.
  - **Treiber:** Guard `NotifyVar` (`strategy:"switch"`, `switch_entity:"switch.test_template_switch"`, Health `input_boolean.test_1`, `behavior:{debounce:5, cooldown:5}`) mit `notify_action` = Aktion, die `input_text.test_note` setzt: `notify_action:[{"action":"input_text.set_value","data":{"entity_id":"input_text.test_note","value":"{{ event }}|{{ message }}"}}]`. Health brechen (`input_boolean.turn_off test_1`), Debounce ablaufen lassen (`N.wait(7)`).
  - **Assert:** `N.st("input_text.test_note")["state"]` enthält Event-Key + lokalisierte Meldung, z. B. beginnt mit `"recovery_attempt|NotifyVar: Reparaturversuch 1 von 2."` (de-Sprache). (Bei Auto-aus-Variante stattdessen `no_auto_recovery|NotifyVar: Problem erkannt, Auto-Reparatur ist deaktiviert.`)
  - **Cleanup:** `N.delete_subentry(entry, sub)` + `N.call("input_boolean","turn_on", entity_id="input_boolean.test_1")`

- [ ] **NA3 — Lokalisierte de-Meldungen aus `NOTIFY_MESSAGES`** · `P0`
  - **Prüft:** Bei `language=de` rendert `{{ message }}` die deutschen Texte für `recovery_attempt/success/failed/blocked/no_auto_recovery/problem_detected`.
  - **Files:** `const.py` `NOTIFY_MESSAGES` `de`-Block (Texte OHNE Name-Präfix = `event_text`; z. B. `recovery_attempt`=`"Reparaturversuch {attempt} von {max}."`, `recovery_success`=`"Reparatur erfolgreich."`, `recovery_failed`=`"Reparatur fehlgeschlagen nach {attempts}."`, `no_auto_recovery`=`"Problem erkannt, Auto-Reparatur ist deaktiviert."`); `core/notify.py` → `_resolve` Sprachauswahl `hass.config.language` mit en-Fallback.
  - **Treiber:** Wie NA2; einmal Auto-aus erzwingen (→ `no_auto_recovery`), einmal heilbare `*_check`-Recovery (→ `recovery_success`, vgl. CC8). `input_text.test_note` jeweils prüfen.
  - **Assert:** `test_note` enthält exakt `"Problem erkannt, Auto-Reparatur ist deaktiviert."` bzw. `"Reparatur erfolgreich."` (deutsche Strings aus `NOTIFY_MESSAGES["de"]`, NICHT englisch).
  - **Cleanup:** `N.delete_subentry(entry, sub)` + Health zurücksetzen.

- [ ] **NA4 — Fehlender Notify-Service → gefangen, kein Crash** · `P0`
  - **Prüft:** Eine Notify-Aktion mit nicht existierendem Service wird gefangen (geloggt), der Guard läuft normal weiter, kein Traceback bricht die State-Machine.
  - **Files:** `core/notify.py` `_run` fängt `vol.Invalid` (`"Notify action invalid for …"`) bzw. `Exception` (`LOGGER.exception("Notify action failed for …")`); detached via `hass.async_create_task`.
  - **Treiber:** Guard `NotifyMiss` (`strategy:"switch"`, `switch_entity:"switch.test_template_switch"`, Health `input_boolean.test_1`, `behavior:{debounce:5, cooldown:5}`) mit `notify_action:[{"action":"notify.does_not_exist","data":{"message":"{{ message }}"}}]`. Health brechen + `N.wait(7)`.
  - **Assert:** `N.log()` enthält `"Notify action invalid for NotifyMiss"` ODER `"Notify action failed for NotifyMiss"`; `N.g("/api/config")["state"] == "RUNNING"`; `N.guard("notifymiss")[0]` ist ein gültiger State (z. B. `suspect`/`recovering`/`escalated`), nicht `None`.
  - **Cleanup:** `N.delete_subentry(entry, sub)` + Health zurücksetzen.

- [ ] **NA5 — Variablen-Hinweis in Section-`description` (de), keine `{{ }}`-Klammern als ICU** · `P1`
  - **Prüft:** Der Variablen-Hinweis steht in der `notification`-Section-`description` (nicht `data_description`), und de.json verwendet KEINE rohen `{{ }}` (ICU-Falle), sondern die Umschreibung „in doppelten geschweiften Klammern".
  - **Files:** `translations/de.json` `notification.description` = `"Optionale Aktion bei Problemen und Reparaturen. Verfügbare Jinja-Variablen (in doppelten geschweiften Klammern): message (die fertige, lokalisierte Meldung), name, event."`.
  - **Treiber:** `grep -n "notification" translations/de.json` → jeder Section-Treffer hat `description`, keine literalen `{{`/`}}`.
  - **Assert:** Datei-Inhalt enthält Phrase `"in doppelten geschweiften Klammern"` und KEIN `{{` in den Notify-Beschreibungen.
  - **Cleanup:** —

### P0 — Corner Cases / Robustheit

- [ ] **CC1 — Kaputtes Jinja im Health-Template wird abgelehnt** · `P0`
  - **Prüft:** Ein syntaktisch defektes Template (`{{ 1 + }}`, unclosed) wird vom `TemplateSelector` validiert und der Device-Step lehnt ab (kein `create_entry`).
  - **Files:** `config_flow_helpers/schemas.py` `_health_fields` (SOURCE_TEMPLATE-Zweig) nutzt `selector.TemplateSelector()` (serverseitige Validierung) für das flache Feld `template` (kein Section-Wrapper mehr).
  - **Treiber:** Subentry-Flow manuell bis `device`-Step treiben und `template = "{{ 1 + }}"` FLACH posten (statt `N.create_guard`, das nur valide Templates kennt): `requests.post(.../subentries/flow, {"handler":[hub,"device"]})` → `_post_flow(fid,{"source_type":"template_based"})` → `_post_flow(fid,{"name":"BadJinja","template":"{{ 1 + }}"})` (kein `mode` im Device-Step).
  - **Assert:** Antwort enthält `errors` (z. B. `errors["template"]`/`base`) bzw. bleibt `step_id=="device"`; KEIN `type=="create_entry"`.
  - **Cleanup:** — (kein Subentry angelegt)

- [ ] **CC2 — Template-Verdicts: UNKNOWN macht keinen Fehlalarm** · `P0`
  - **Prüft:** Whitelist: `true`/`on`/`1`/`yes`→OK · `false`/`off`/`0`/`no`/`is_state→False`→UNHEALTHY · alles übrige (`'banana'`, `42`, `states(missing)`, `none`, leer, Render-Fehler)→UNKNOWN (kein Recover, kein SUSPECT).
  - **Files:** `core/health/template.py` `_HEALTHY_RESULTS={"true","on","1","yes"}` / `_FAULTY_RESULTS={"false","off","0","no"}`; `evaluate()`: `TemplateError`→UNKNOWN, sonst `str(result).strip().lower()` gegen die zwei Sets, alles übrige→UNKNOWN.
  - **Treiber:** Guard `TplUnknown` mit `source_type:"template_based"`, `health:{template:"{{ states('sensor.does_not_exist') }}"}`, `strategy:"switch"`, `switch_entity:"switch.test_template_switch"`, `behavior:{debounce:5, cooldown:5}`. `N.wait(8)` → `N.guard("tplunknown")`.
  - **Assert:** `N.guard("tplunknown")[0] == "ok"` (UNKNOWN bleibt OK, kein `suspect`); `binary_sensor.tplunknown_gesundheit` ist NICHT `off`. Gegenprobe: Reconfigure auf `{{ false }}` → nach Debounce `suspect`/`escalated`.
  - **Cleanup:** `N.delete_subentry(entry, sub)`

- [ ] **CC3 — Health-Entität unavailable/unknown (state_based) → UNKNOWN, kein Recover** · `P0`
  - **Prüft:** Eine state_based-Health-Entität in `unavailable`/`unknown` ergibt UNKNOWN (kein Fehlalarm), solange `unavailable` nicht explizit in `off_value` steht.
  - **Files:** `core/health/entity_state.py` ambivalente States (`unavailable`/`unknown`) → UNKNOWN; explizites `off_value` gewinnt (auch über unavailable).
  - **Treiber:** Guard `StateUnknown` mit `health:{entity_id:"binary_sensor.test_reachable", on_value:["on"], off_value:["off"]}`, `strategy:"switch"`, `switch_entity:"switch.test_template_switch"`, `behavior:{debounce:5, cooldown:5}`. `N.setstate("binary_sensor.test_reachable","unavailable")` → `N.wait(8)` → `N.guard("stateunknown")`.
  - **Assert:** `N.guard("stateunknown")[0] == "ok"` (nicht `suspect`); kein `"StateUnknown unhealthy, waiting"`-Log für diesen Guard.
  - **Cleanup:** `N.delete_subentry(entry, sub)` + `N.setstate("binary_sensor.test_reachable","on")`

- [ ] **CC4 — Switch fehlt (switch_cycle) → `can_recover` blockt → ESCALATED `recovery_blocked`** · `P0`
  - **Prüft:** Fehlende Switch-Entity wird in `can_recover` erkannt → Engine eskaliert mit `recovery_blocked`, kein blindes Schalten.
  - **Files:** `core/drivers/switch_cycle.py` `can_recover` → `LOGGER.error("Switch entity %s not found")` + `return False, …`; `core/engine.py` `not ok` → `LOGGER.warning("%s recovery blocked: %s")` + `_escalate("recovery_blocked", reason=reason)`.
  - **Treiber:** Guard `SwMissing` mit `strategy:"switch"`, `switch_entity:"switch.ganz_sicher_weg"`, Health `input_boolean.test_1`, `behavior:{debounce:5, cooldown:5}`. Health brechen (`input_boolean.turn_off test_1`) → `N.wait(8)` → `N.guard("swmissing")`.
  - **Assert:** `N.guard("swmissing")[0] == "escalated"`; `N.log()` enthält `"Switch entity switch.ganz_sicher_weg not found"` und `"recovery blocked"`.
  - **Cleanup:** `N.delete_subentry(entry, sub)` + `N.call("input_boolean","turn_on", entity_id="input_boolean.test_1")`

- [ ] **CC5 — poe_port: kein/mehrdeutiger Match → blockt → ESCALATED `recovery_blocked`** · `P0`
  - **Prüft:** Lässt sich `expected_id` nicht auf genau einen Port auflösen, blockt `can_recover` über die Fabric → Eskalation; eine leere Portliste ergänzt beim Start zusätzlich einen `config_errors`-Log.
  - **Files:** `core/drivers/poe_port.py` `can_recover` → `fabric.resolve_with_reason` → `port is None` → `LOGGER.error("PoE %s: %s")` + `return False, reason`; `core/poe.py` Laufzeit-Reason `"no port matches '<id>'"`; `core/drivers/poe_port.py` `config_errors` „no ports configured" wenn `port_count==0` (Startup-Check via `engine._check_config`).
  - **Treiber:** Guard `PoeNomatch` mit `strategy:"poe_port"`, `expected_id:"zz:zz:zz:zz:zz:zz"` (kein realer Port), Health `input_boolean.test_1`, `behavior:{debounce:5, cooldown:5, boot_window:5, max_attempts:2}`. Health brechen → `N.wait(8)` → `N.guard("poenomatch")`.
  - **Assert:** `N.guard("poenomatch")[0] == "escalated"`; `N.log()` enthält `"PoE zz:zz:zz:zz:zz:zz"` und `"recovery blocked"`. (Bei leerer Portliste enthält das Log zusätzlich aus dem Startup-Check `"no ports configured"`; das ist NICHT der Laufzeit-Reason.)
  - **Cleanup:** `N.delete_subentry(entry, sub)` + `N.call("input_boolean","turn_on", entity_id="input_boolean.test_1")`

- [ ] **CC6 — Action-Service fehlt OHNE Health-Check → kein falscher Erfolg → Retry → ESCALATED** · `P0`
  - **Prüft:** `can_recover` validiert nur die Struktur (kein Service-Existenz-Check), also läuft `recover()` an, wirft beim unbekannten Service (`ServiceNotFound`) und wird als fehlgeschlagener Versuch behandelt — NICHT `recover_success` — und bis `max_attempts` retried, dann `recovery_failed`.
  - **Files:** `core/drivers/action_call.py` `can_recover` validiert via `async_validate` (nur Schema, keine Service-Existenz); `core/engine.py` `try: await self.driver.recover()` → `except Exception` → `LOGGER.exception("Recovery driver failed for %s")` → Retry/`_escalate()`.
  - **Treiber:** Guard `ActMissing` mit `strategy:"action"`, `action:[{"action":"script.gibt_es_nicht"}]`, Health `input_boolean.test_1`, `behavior:{debounce:5, cooldown:5}` (action ohne check → `max_attempts` Default 2). Health brechen → `N.wait(10)` → `N.guard("actmissing")`.
  - **Assert:** `N.guard("actmissing")[0] == "escalated"`; `attrs["recover_count"] == 0`; `N.log()` enthält `"Recovery driver failed for ActMissing"` (mind. 1×) und am Ende `"ActMissing could not be recovered"`. KEIN `"recovered after"` für diesen Guard.
  - **Cleanup:** `N.delete_subentry(entry, sub)` + `N.call("input_boolean","turn_on", entity_id="input_boolean.test_1")`

- [ ] **CC7 — Action-Service heilt nicht MIT Health-Check → Verify schlägt fehl → Retry/Escalate** · `P0`
  - **Prüft:** Bei `action_check` und nicht heilender Aktion bleibt Health unhealthy → VERIFY-Timeout → Retry bis `max_attempts` → ESCALATED.
  - **Files:** `core/engine.py` `_set_state(VERIFY)` → `_wait_health_ok(boot_window)` → False → bei `attempt >= max_attempts` `_escalate()`.
  - **Treiber:** Guard `ActCheckMiss` mit `strategy:"action_check"`, `action:[{"action":"input_text.set_value","data":{"entity_id":"input_text.test_note","value":"x"}}]` (heilt Health NICHT), Health `input_boolean.test_1`, `behavior:{debounce:5, cooldown:5, boot_window:5, max_attempts:2}`. Health brechen → `N.wait(5 + 2*5 + 4)` → `N.guard("actcheckmiss")`.
  - **Assert:** `N.guard("actcheckmiss")[0] == "escalated"`; `attrs` zeigt `attempt==2`, `recover_count==0`; `N.log()` enthält `"could not be recovered after 2"`.
  - **Cleanup:** `N.delete_subentry(entry, sub)` + `N.call("input_boolean","turn_on", entity_id="input_boolean.test_1")`

- [ ] **CC8 — Heilbare `*_check`-Recovery → VERIFY greift → COOLDOWN (kein Fehlalarm-Loop)** · `P1`
  - **Prüft:** Eine `*_check`-Aktion, die die Health-Entität wieder gesund schaltet, durchläuft VERIFY→COOLDOWN sauber (`recover_count=1`); der Heil-Trick aus dem Runbook.
  - **Files:** `core/engine.py` `_wait_health_ok` True → `_recover_success()` → `LOGGER.info("%s recovered after %s attempt(s)…")` + `COOLDOWN`.
  - **Treiber:** Guard `HealOK`, Health `input_boolean.test_5` (on=gesund), `strategy:"action_check"`, `action:[{"action":"input_boolean.turn_on","data":{"entity_id":"input_boolean.test_5"}}]`, `behavior:{debounce:5, cooldown:30, boot_window:10, max_attempts:2}`. `N.call("input_boolean","turn_off", entity_id="input_boolean.test_5")` → `N.wait(11)` → `N.guard("healok")`.
  - **Assert:** `N.guard("healok")[0] == "cooldown"`; `attrs["recover_count"] == 1`; `N.log()` enthält `"recovered after 1 attempt(s)"`.
  - **Cleanup:** `N.delete_subentry(entry, sub)` + `N.call("input_boolean","turn_on", entity_id="input_boolean.test_5")`

- [ ] **CC9 — Recovery-Aktion schaltet Health AUS → Loop bounded durch max_attempts** · `P1`
  - **Prüft:** Eine kontraproduktive `*_check`-Aktion (macht Health unhealthy) führt nicht zur Endlosschleife — sie ist durch `max_attempts`→ESCALATED begrenzt.
  - **Files:** `core/engine.py` `attempt >= max_attempts` → `_escalate()`; VERIFY-Pfad.
  - **Treiber:** Guard `HealLoop`, Health `input_boolean.test_6`, `strategy:"action_check"`, `action:[{"action":"input_boolean.turn_off","data":{"entity_id":"input_boolean.test_6"}}]` (hält Health unten), `behavior:{debounce:5, cooldown:5, boot_window:5, max_attempts:2}`. `N.call("input_boolean","turn_off", entity_id="input_boolean.test_6")` → `N.wait(5 + 2*5 + 5)` → `N.guard("healloop")`.
  - **Assert:** `N.guard("healloop")[0] == "escalated"` mit `attrs["attempt"]==2` (terminale Grenze, kein Dauer-Cycle); `N.log()` enthält `"could not be recovered after 2"`.
  - **Cleanup:** `N.delete_subentry(entry, sub)` + `N.call("input_boolean","turn_on", entity_id="input_boolean.test_6")`

- [ ] **CC10 — Leere Aktion(en) beim Submit abgelehnt (F6)** · `P1`
  - **Prüft:** Strategie `action`/`actions` ohne Inhalt wird beim Submit mit `action_required` abgelehnt (nicht erst zur Laufzeit).
  - **Files:** `config_flow.py` `errors[CONF_ACTION]="action_required"` (action-Step); `errors[CONF_OFF_ACTION]`/`[CONF_ON_ACTION]="action_required"` (actions-Step).
  - **Treiber:** Subentry-Flow bis Strategie-Step treiben, `strategy:"action"`, dann `action`-Step mit `action: []` (leer) posten.
  - **Assert:** Antwort enthält `errors["action"] == "action_required"`; KEIN `create_entry`.
  - **Cleanup:** — (kein Subentry)

---

## Sections & Flatten · Entity-Exclusion · PoE Options-Flow

### Sections & Flatten

> **Helper für `.storage`-Reads** (mehrfach genutzt): Die Config-Entry-REST-API liefert **kein** `options`/`data`/`subentries`-Feld (`as_json_fragment` in `homeassistant/config_entries.py` enthält nur Metadaten + `num_subentries`). Ports/Driver flach prüfen daher über die Storage-Datei (wie `N.hub_id()`):
> ```python
> import json
> def _ports():  # flache Portliste aus dem Storage
>     ce=json.load(open("<ha-core>/config/.storage/core.config_entries"))
>     e=[e for e in ce["data"]["entries"] if e["domain"]=="necromancer"][0]
>     return e.get("options",{}).get("ports",[])
> def _subs():   # Subentry-data (inkl. driver/health) aus dem Storage
>     ce=json.load(open("<ha-core>/config/.storage/core.config_entries"))
>     return [e for e in ce["data"]["entries"] if e["domain"]=="necromancer"][0]["subentries"]
> ```
> Storage wird verzögert geschrieben → vor dem Read **`N.wait(2)`**. Sofort/robust beobachtbar ist außerdem `N.remove_port(label)` (findet das Label nur, wenn der Port flach unter Top-Level-`label` gespeichert wurde).

- [ ] **SF1 — Sektionen serverseitig ausgeklappt** · `P1`
  - **Prüft:** Nicht-collapsed Sektionen melden dem Frontend `expanded:true` (Default `collapsed=False`); nur `linked_guards` ist collapsed.
  - **Files:** `config_flow_helpers/schemas.py` → `_section`: `section(vol.Schema(fields), {"collapsed": collapsed})` mit `collapsed: bool = False`; `_link_section` ist die EINZIGE Sektion mit `collapsed=True`. Serverseitige Übersetzung in `homeassistant/helpers/config_validation.py` Section → `{"type":"expandable","expanded": not collapsed}`.
  - **Treiber:** Flow bis Step `switch` treiben (testkit-intern): `hub=N.hub_id()`; `r=N._post_flow`-Kette ist nur in `create_guard` gekapselt — hier manuell: POST `/api/config/config_entries/subentries/flow` mit `{"handler":[hub,"device"]}` → `fid`; `N._post_flow(fid,{"source_type":"state_based"})` (→ `device`); `N._post_flow(fid,{"name":"SecX","entity_id":"binary_sensor.test_reachable","on_value":["on"],"off_value":["off"]})` (→ `strategy`; Device-Step flach, kein `mode`-Feld); `N._post_flow(fid,{"strategy":"switch"})` (→ `switch`). Im zurückgegebenen `data_schema` die Felder mit `name=="behavior"` / `name=="notification"` suchen.
  - **Assert:** Im `switch`-Schema hat das Feld `behavior` `"type":"expandable"` und `"expanded": true`; das Feld `notification` ebenfalls `"expanded": true`; ein evtl. vorhandenes `linked_guards`-Feld (nur wenn ein ANDERER Recover-Guard existiert) trägt `"expanded": false`.
  - **Cleanup:** Flow ohne Save verwerfen: `requests.delete(f"http://localhost:8123/api/config/config_entries/subentries/flow/{fid}",headers=N.H)`.

- [ ] **SF2 — _flatten_sections hebt verschachtelte Werte hoch (Device-Create)** · `P1`
  - **Prüft:** Submit-Form `{section:{feld:…}}` wird vor Verwendung flachgezogen, sodass der Guard real entsteht (Recover-Step-Sektionen + die noch verschachtelt postenden Testkit-Device-Keys verarbeitet).
  - **Files:** `config_flow_helpers/schemas.py` → `_flatten_sections` `out.update(value)` für jedes dict; aufgerufen in `config_flow.py` `async_step_device` und `async_step_add_port` sowie in `config_flow_helpers/schemas.py` `_build_data`.
  - **Treiber:** `hub,sub=N.create_guard({"source_type":"state_based","name":"FlatDev","health":{"entity_id":"binary_sensor.test_reachable","on_value":["on"],"off_value":["off"]},"mode":"recover","strategy":"switch","switch_entity":"switch.test_template_switch","behavior":{"debounce":5,"cooldown":5}})` — der Device-Step ist FLACH, aber das Testkit postet aus Altgründen noch verschachtelt (`state_check`/`assigned_device`), was `_flatten_sections` hochzieht; die Switch-Step-Sektionen `behavior`/`notification` werden ebenso geflattet.
  - **Assert:** `N.st("sensor.flatdev_status")` ist nicht `None` (Guard entstand → verschachtelte Werte wurden geflattet/verarbeitet). Der Switch-Wert kam aus der `power`-losen Flat-Form: `N.guard("flatdev")[1]["target"] == "switch.test_template_switch"` (Status-Sensor-Attr `target`=`driver.target_info()`). Health beobachtbar: `N.setstate("binary_sensor.test_reachable","off")`; `N.wait(1)`; `N.st("binary_sensor.flatdev_gesundheit")["state"]=="off"`. (Es gibt **kein** `health`-Attribut am Status-Sensor — Attrs sind nur `attempt/recover_count/last_recover/last_seen/target/auto_restart`.)
  - **Cleanup:** `N.delete_subentry(hub,sub)`; `N.setstate("binary_sensor.test_reachable","on")`.

- [ ] **SF3 — _flatten_sections beim Port-Add (Options-Flow)** · `P1`
  - **Prüft:** Die 4 Port-Sektionen (`power`/`identity`/`status`/`timing`) werden beim Add flachgezogen und FLACH persistiert.
  - **Files:** `config_flow.py` → `async_step_add_port` ruft `_flatten_sections(user_input)`; `config_flow_helpers/schemas.py` → `_port_schema` baut die 4 Sektionen.
  - **Treiber:** `N.add_port({"label":"FlatPort","actuator":"input_boolean.sim_poe_port","status_entity":"binary_sensor.test_reachable","status_on":["on"],"status_off":["off"],"off_on_delay":2,"off_timeout":10,"on_timeout":20})`.
  - **Assert:** Robust: `N.remove_port("FlatPort")` würde ihn finden (Label flach gespeichert) — siehe Cleanup. Flach-Struktur konkret via Storage: `N.wait(2)`; im `_ports()`-Helper existiert ein Dict mit `label=="FlatPort"`, dessen Keys `actuator`/`status_entity`/`off_on_delay`/`off_timeout`/`on_timeout` **auf Top-Level** liegen (KEINE Subdicts `power`/`status`/`timing`).
  - **Cleanup:** `N.remove_port("FlatPort")`.

### Entity-Selektor-Exclusion

- [ ] **EX1 — Health-Picker: nur EIGENE Guard-Entities ausgeschlossen, fremde wählbar** · `P1`
  - **Prüft:** Im Health-Entity-Selektor sind nur die Entities **des gerade bearbeiteten** Guards ausgeschlossen (kein Self-Loop). Entities **anderer** Guards bleiben wählbar → **Supervisor-/Staged-Guards**. Beim Neuanlegen (noch keine subentry_id) wird necromancer-seitig nichts ausgeschlossen.
  - **Files:** `config_flow_helpers/schemas.py` → `_own_guard_entities(hass, subentry_id)` (filtert `platform==DOMAIN AND unique_id.startswith(subentry_id)`; `[]` ohne id); im Health-Picker via `async_step_device` `exclude=_own_guard_entities(self.hass, self._own_subentry_id())`. (Switch/Port nutzen weiter `_own_entities` = ALLE necromancer-Entities, siehe EX2.) Automatisiert: `test_units.py::test_own_guard_entities_only_self`.
  - **Treiber:** Zwei Guards „A"/„B" anlegen; Guard A **reconfigure**n, Device-Step-Schema holen, das flache Feld `entity_id` → `selector.entity.exclude_entities` lesen (Top-Level, kein `state_check`-Wrapper mehr).
  - **Assert:** Enthält `sensor.a_status` (eigene), aber **NICHT** `sensor.b_status` (fremde → wählbar).
  - **Cleanup:** Flow verwerfen; Guards löschen.

- [ ] **EX2 — Exclusion auch im Port-Formular (Actuator/Id/Status)** · `P1`
  - **Prüft:** Eigene Entities sind auch im PoE-Port-Add-Formular gefiltert.
  - **Files:** `config_flow_helpers/schemas.py` → `_port_schema` (`actuator`/`id_entity`/`status_entity`) je `_entity_selector(exclude,…)`; `config_flow.py` → `async_step_add_port` übergibt `exclude=_own_entities(self.hass)`.
  - **Treiber:** Mit existierendem Guard `ExcludeMe`: `_entry,fid,r=N._opt_start()` (Menu); `r=N._opt_post(fid,{"next_step_id":"add_port"})` (Step `add_port`); im `data_schema` die expandable-Felder `power`/`identity`/`status` → deren `schema` → Felder `actuator`/`id_entity`/`status_entity`.
  - **Assert:** In `actuator`, `id_entity`, `status_entity` enthält `selector.entity.exclude_entities` den Eintrag `switch.excludeme_auto_reparatur`.
  - **Cleanup:** Options-Flow ohne Save verwerfen: `requests.delete(f"http://localhost:8123/api/config/config_entries/options/flow/{fid}",headers=N.H)`; `ExcludeMe`-Guard löschen.

### Multiselect-Werte (Health + Port-Status)

- [ ] **MS1 — Health on/off als Listen: booting∈on→OK, error∈off→UNHEALTHY** · `P1`
  - **Prüft:** Mehrwertige `on_value`/`off_value`-Listen werden korrekt zu Verdikten gemappt.
  - **Files:** `config_flow_helpers/selectors.py` → `_HEALTH_VALUE_SELECTOR=_LiveStateSelector(...,multiple=True)` (genutzt in `config_flow_helpers/schemas.py`), `_watch_fields` (schemas.py) baut `on_value`/`off_value` als Listen; Auswertung in `core/health/entity_state.py` → `evaluate`: `off`-Treffer→`UNHEALTHY`, `on`-Treffer→`OK`, sonst `UNKNOWN`.
  - **Treiber:** `hub,sub=N.create_guard({"source_type":"state_based","name":"MultiSel","health":{"entity_id":"input_select.test_state","on_value":["online","booting"],"off_value":["offline","error"]},"mode":"notify","behavior":{"debounce":0}})`. Dann `N.call("input_select","select_option",entity_id="input_select.test_state",option="booting")`; `N.wait(1)`; `N.st("binary_sensor.multisel_gesundheit")`. Danach `N.call("input_select","select_option",entity_id="input_select.test_state",option="error")`; `N.wait(1)`; erneut lesen.
  - **Assert:** `N.st("binary_sensor.multisel_gesundheit")["state"]=="on"` bei `booting` (booting∈on→OK→connectivity-`is_on`); `=="off"` bei `error` (error∈off→UNHEALTHY). (Der Status-Sensor hat **kein** `health`-Attribut; nur das Gesundheit-Binary-Sensor-State ist die Quelle.)
  - **Cleanup:** `N.delete_subentry(hub,sub)`; `N.call("input_select","select_option",entity_id="input_select.test_state",option="online")`.

- [ ] **MS2 — Port-Status status_on/status_off als Listen** · `P1`
  - **Prüft:** `status_on`/`status_off` werden als Listen gespeichert (`_STATUS_VALUE_SELECTOR` multiple).
  - **Files:** `config_flow_helpers/selectors.py` → `_STATUS_VALUE_SELECTOR=_LiveStateSelector(CONF_STATUS_ENTITY,CONF_STATUS_ATTRIBUTE)`, `config_flow_helpers/schemas.py` → `_port_schema` `status_on`/`status_off` default `_as_list(...) or ["on"]/["off"]`.
  - **Treiber:** `N.add_port({"label":"MultiStat","actuator":"input_boolean.sim_poe_port","status_entity":"input_select.test_state","status_on":["online","booting"],"status_off":["offline","error"],"off_on_delay":2,"off_timeout":10,"on_timeout":20})`.
  - **Assert:** `N.wait(2)`; im `_ports()`-Helper hat der Port `MultiStat` `status_on==["online","booting"]` und `status_off==["offline","error"]` (2-elementige Listen, FLACH). Zusätzlich findbar: `N.remove_port("MultiStat")`→`{"removed":True}`.
  - **Cleanup:** `N.remove_port("MultiStat")`.

### Jede Step-Beschreibung vorhanden

- [ ] **DESC1 — Alle Subentry-Steps haben eine Beschreibung** · `P1`
  - **Prüft:** user/reconfigure/device/strategy/switch/action/actions/poe_port/notify tragen je `description` (verifiziert in `translations/en.json` UND `translations/de.json`).
  - **Files:** `translations/en.json` + `translations/de.json` → `config_subentries.device.step.<step>.description`.
  - **Treiber:** Aus `custom_components/necromancer`:
    `python3 -c "import json;[print(f,[k for k in ['user','reconfigure','device','strategy','switch','action','actions','poe_port','notify'] if not json.load(open(f))['config_subentries']['device']['step'].get(k,{}).get('description')]) for f in ['translations/en.json','translations/de.json']]"`
  - **Assert:** Für `translations/en.json` und `translations/de.json` jeweils leere Liste `[]`.
  - **Cleanup:** —

- [ ] **DESC2 — Alle Options-Steps + add_port-Sektionen haben Beschreibungen** · `P1`
  - **Prüft:** init/add_port/edit_port/delete_port/import_ports/export_ports/export_result und die 4 Port-Sektionen tragen `description`.
  - **Files:** `translations/en.json`/`translations/de.json` → `options.step.<step>.description` und `options.step.add_port.sections.{power,identity,status,timing}.description`.
  - **Treiber:**
    `python3 -c "import json;s=json.load(open('translations/en.json'));o=s['options']['step'];print([k for k in ['init','add_port','edit_port','delete_port','import_ports','export_ports','export_result'] if not o.get(k,{}).get('description')],[k for k in ['power','identity','status','timing'] if not o['add_port']['sections'].get(k,{}).get('description')])"` — gleich für `translations/de.json`.
  - **Assert:** Ausgabe `[] []` (für translations/en.json und de.json).
  - **Cleanup:** —

### PoE: flache Portliste / Options-Flow

- [ ] **POE1 — Options-Flow: Menü + Ports flach in entry.options** · `P1`
  - **Prüft:** Add→Save schreibt die Ports flach nach `entry.options[CONF_PORTS]`.
  - **Files:** `config_flow.py` → `NecromancerOptionsFlow.async_step_init` (Menü add/edit/delete/import/export/save), `async_step_save` `async_create_entry(data={CONF_PORTS:self._ports})`.
  - **Treiber:** `N.add_port({"label":"OptPort","actuator":"input_boolean.sim_poe_port","status_entity":"binary_sensor.test_reachable"})`; dann `N.wait(2)`.
  - **Assert:** Im `_ports()`-Helper enthält die Liste ein Dict mit `label=="OptPort"`, dessen Felder (`actuator`,`status_entity`,`off_on_delay`,…) TOP-LEVEL liegen (nicht unter `power`/`timing`). (REST `/api/config/config_entries/entry/{id}` liefert KEIN `options` → Storage-Read nötig.)
  - **Cleanup:** `N.remove_port("OptPort")`.

- [ ] **POE2 — edit_port ersetzt (statt anhängt)** · `P1`
  - **Prüft:** Editieren eines Ports ersetzt den Eintrag am selben Index, erhöht die Portzahl nicht.
  - **Files:** `config_flow.py` → `async_step_edit_port` setzt `_edit_index`+`_editing=True`→`async_step_add_port`; `async_step_add_port` `if self._editing: self._ports[self._edit_index]=port` (Replace statt `append`).
  - **Treiber:** Port anlegen `N.add_port({"label":"EditMe","actuator":"input_boolean.sim_poe_port","status_entity":"binary_sensor.test_reachable","off_on_delay":2})`. Options-Flow manuell: `_entry,fid,r=N._opt_start()`; `r=N._opt_post(fid,{"next_step_id":"edit_port"})`; aus `r["data_schema"]` das `port`-Select nach Option mit `label=="EditMe"` durchsuchen und dessen `value` holen; `r=N._opt_post(fid,{"port":<value>})` (→ Step `add_port` mit vorbefüllten Sektionen); das **vollständige Sektions-Payload** posten (wie `N.add_port` es baut: `{"label":"EditMe","power":{"actuator":"input_boolean.sim_poe_port"},"identity":{},"status":{"status_entity":"binary_sensor.test_reachable","status_on":["on"],"status_off":["off"]},"timing":{"off_on_delay":5,"off_timeout":10,"on_timeout":20}}`) → `menu`; `N._opt_post(fid,{"next_step_id":"save"})` (→ `create_entry`); `N.wait(2)`.
  - **Assert:** Im `_ports()`-Helper genau EIN Eintrag mit `label=="EditMe"` und `off_on_delay==5` (ersetzt, nicht dupliziert). `N.remove_port("EditMe")`→`{"removed":True}`, danach `_ports()` enthält kein `EditMe` mehr.
  - **Cleanup:** `N.remove_port("EditMe")` (falls oben nicht schon entfernt).

- [ ] **POE3 — delete_port entfernt korrekt** · `P1`
  - **Prüft:** Delete entfernt genau den gewählten Port per Index.
  - **Files:** `config_flow.py` → `async_step_delete_port` `self._ports.pop(index)`; `N.remove_port` (testkit) treibt `delete_port`→`save`.
  - **Treiber:** `N.add_port({"label":"DelMe","actuator":"input_boolean.sim_poe_port","status_entity":"binary_sensor.test_reachable"})`; `N.remove_port("DelMe")`.
  - **Assert:** Erster `N.remove_port("DelMe")`→`{"removed":True}`; ein zweiter `N.remove_port("DelMe")`→`{"removed":False}` (Port ist weg).
  - **Cleanup:** — (Port bereits entfernt)

- [ ] **POE4 — poe_port-Guard: expected_id sucht die ganze flache Liste (keine Areas)** · `P1`
  - **Prüft:** Ein `poe_port`-Guard referenziert keine Area, nur `expected_id`; der Driver kommt bei vorhandenen Ports ohne `config_error` hoch.
  - **Files:** `config_flow_helpers/schemas.py` → `_build_driver` `{type:"poe_port",expected_id:…}` (KEIN Area-Feld); `_poe_schema` nur `expected_id`+behavior+notify. Driver `core/drivers/poe_port.py` `config_errors`: Marker `poe_port '<id>': no ports configured` nur bei `port_count==0`; geloggt in `core/engine.py` als `"%s: %s"` (Name + err).
  - **Treiber:** Port mit Static-Id anlegen: `N.add_port({"label":"P4","actuator":"input_boolean.sim_poe_port","status_entity":"binary_sensor.test_reachable","id_static":"dev-xyz"})`. Guard: `hub,sub=N.create_guard({"source_type":"state_based","name":"PoeG","health":{"entity_id":"binary_sensor.test_reachable","on_value":["on"],"off_value":["off"]},"mode":"recover","strategy":"poe_port","expected_id":"dev-xyz","behavior":{"debounce":0,"cooldown":2,"boot_window":5,"max_attempts":1}})`. `N.wait(2)`.
  - **Assert:** `N.st("sensor.poeg_status")` ist nicht `None`. Driver-Form via Storage: im `_subs()`-Helper hat der Subentry `PoeG` `data["driver"]=={"type":"poe_port","expected_id":"dev-xyz"}` — KEIN Area-Key. Kein Config-Fehler: `N.log()` enthält NICHT `PoeG: poe_port 'dev-xyz': no ports configured` (Ports sind vorhanden).
  - **Cleanup:** `N.delete_subentry(hub,sub)`; `N.remove_port("P4")`.

DELETED CLAIMS (alle 3 bestätigt obsolet/fehlplatziert — NICHT wiederhergestellt):
1. "Reaktive Selektoren in Sections folgen entity_id/id_entity/status_entity" (REGRESSION.md) — die Live-Nachführung passiert nur im Browser (`ha-form-expandable` regeneriert den Context). Server liefert nur die statische `context`-Map (`_LiveAttributeSelector.serialize`/`_LiveStateSelector.serialize`, config_flow_helpers/selectors.py). Die reaktive Invariante ist per REST nicht beobachtbar → agent-untestbar. Gehört außerdem zum Selektor-/UX-Bereich.
2. "Self-/Cross-Device-Link blockiert (no_self_link)" (REGRESSION.md, offenes `[ ]`) — basiert auf DeviceSelector + `_is_own_device` (config_flow.py) mit `errors[CONF_DEVICE_ID]="no_self_link"`, NICHT auf `exclude_entities`. Gehört in den Linking-/Device-Link-Bereich, nicht in die Entity-Exclusion.
3. "Per-Port asyncio.Lock: test_per_port_lock_serialises/test_driver_and_service_share_lock" (REGRESSION.md) — gehört in den PoE-Fabric-Bereich; zudem obsolet: Per-Port-Lock durch Coalescing (`_inflight`-Task + `asyncio.shield`, core/poe.py) ersetzt; die genannten Tests existieren nicht mehr (jetzt `test_concurrent_callers_coalesce`/`test_driver_and_service_coalesce`).

---

## Persistenz (Store) · Health-Robustheit · Config-Error-Logging

### P0 — Persistenz (Store)

> Store-Datei: `<ha-core>/config/.storage/necromancer.<entry_id>` (`entry_id` via `N.hub_id()`).
> Snapshot-Felder (core/engine.py `snapshot`): `state, attempt, recover_count, last_recover, last_seen, auto, snooze_until`.
> Restore-Regel (core/engine.py `_apply_persisted`): Stats (`recover_count`/`last_recover`/`last_seen`) + `auto` IMMER; `state` NUR wenn `ESCALATED` (bzw. `SNOOZED`); transiente States werden verworfen und aus Live-Health neu abgeleitet.
> Speicher ist verzögert (`SAVE_DELAY=5`, const.py) → vor Restart `N.wait(7)` bzw. Unload flusht synchron (`async_unload_entry` → `store.async_save(serialize())`, __init__.py).
> RESTART-Helfer (Code-/Store-Last neu laden): `pkill -9 -f "[h]omeassistant -c"; relaunch; poll /api/config==RUNNING`.

- [ ] **PERS-1 — ESCALATED überlebt Neustart, kein Re-Attempt** · `P0`
  - **Prüft:** Ein deterministisch-krankes Guard, das eskaliert ist, kommt nach Neustart wieder als `escalated` hoch und versucht KEINE neue Reparatur (`recover_count` bleibt 0).
  - **Files:** core/engine.py `_apply_persisted` (nur `ESCALATED` wird restored) + `snapshot`.
  - **Treiber:** `N.create_guard({source_type:"template_based", name:"PersEsc", health:{template:"{{ false }}"}, mode:"recover", strategy:"action_check", action:[{"service":"input_text.set_value","data":{"entity_id":"input_text.test_note","value":"x"}}], behavior:{debounce:3,cooldown:5,boot_window:4,max_attempts:1}})` → `N.wait(12)` (debounce→1 Versuch→VERIFY-Timeout→escalated) → `N.guard("persesc")` muss `escalated` zeigen → `N.wait(7)` (Store-Flush) → RESTART → nach RUNNING: `N.guard("persesc")`.
  - **Assert:** Nach Restart `N.guard("persesc")[0] == "escalated"` UND `N.guard("persesc")[1]["recover_count"] == 0`; im `N.log()` NACH Restart kein neuer Marker `"PersEsc recovery attempt"`.
  - **Cleanup:** `N.delete_subentry(N.hub_id(), <sub>)` mit `<sub>` aus dem `create_guard`-Rückgabewert (bzw. Subentry-id aus `N.list_subentries(N.hub_id())` per Titel `"PersEsc"`).

- [ ] **PERS-2 — ESCALATED Auto-Clear bei gesunder Health nach Neustart** · `P0`
  - **Prüft:** Ein eskaliertes Guard, dessen Health beim Hochlauf wieder OK ist, wird via `_evaluate` von `ESCALATED → OK` geräumt (core/engine.py).
  - **Files:** core/engine.py `_evaluate` (`state == ESCALATED and h == Health.OK → attempt=0; _set_state(OK)`).
  - **Treiber:** Guard wie PERS-1, aber state_based gegen schaltbare Health: `N.create_guard({source_type:"state_based", name:"PersClr", health:{entity_id:"input_boolean.test_1", on_value:["on"], off_value:["off"]}, mode:"recover", strategy:"action_check", action:[{"service":"input_text.set_value","data":{"entity_id":"input_text.test_note","value":"x"}}], behavior:{debounce:3,cooldown:5,boot_window:4,max_attempts:1}})` → `N.call("input_boolean","turn_off",entity_id="input_boolean.test_1")` → `N.wait(12)` → `N.guard("persclr")[0]=="escalated"` → `N.call("input_boolean","turn_on",entity_id="input_boolean.test_1")` (Health jetzt OK) → `N.wait(7)` → RESTART → nach RUNNING.
  - **Assert:** Nach Restart `N.guard("persclr")[0] == "ok"` (ESCALATED restored, erste `_evaluate` mit Health=OK räumt → OK).
  - **Cleanup:** Subentry `"PersClr"` löschen (`N.delete_subentry(N.hub_id(), <sub>)`) + `N.call("input_boolean","turn_on",entity_id="input_boolean.test_1")`.

- [ ] **PERS-3 — Stats (`recover_count`) überleben Neustart** · `P0`
  - **Prüft:** Nach einem erfolgreichen Recover ist `recover_count` im Snapshot persistiert und kommt nach Neustart unverändert hoch (core/engine.py `_recover_success` `snapshot`).
  - **Files:** core/engine.py `_recover_success` (`self.recover_count += 1`) + `snapshot` (`"recover_count": self.recover_count`) + `_apply_persisted`.
  - **Treiber:** `N.create_guard({source_type:"state_based", name:"PersStat", health:{entity_id:"input_boolean.test_5", on_value:["on"], off_value:["off"]}, mode:"recover", strategy:"action_check", action:[{"service":"input_boolean.turn_on","data":{"entity_id":"input_boolean.test_5"}}], behavior:{debounce:3,cooldown:4,boot_window:6,max_attempts:2}})` → `N.call("input_boolean","turn_off",entity_id="input_boolean.test_5")` (Health off → Recovery-Aktion schaltet test_5 wieder on → VERIFY grün → COOLDOWN) → `N.wait(10)` → `N.guard("persstat")[1]["recover_count"]` soll `1` sein → `N.wait(7)` (Flush) → RESTART.
  - **Assert:** Nach Restart `N.guard("persstat")[1]["recover_count"] == 1`.
  - **Cleanup:** Subentry `"PersStat"` löschen + `N.call("input_boolean","turn_on",entity_id="input_boolean.test_5")`.

- [ ] **PERS-4 — `auto`-Flag (Auto-Reparatur aus) überlebt Neustart** · `P0`
  - **Prüft:** Der Laufzeit-Switch „Auto-Reparatur" persistiert seinen Wert (core/engine.py `set_auto` `snapshot` restore) und kommt nach Neustart als „aus" hoch.
  - **Files:** core/engine.py `set_auto` (setzt `self.auto` + `self._save()`) + `snapshot` + `_apply_persisted`.
  - **Treiber:** beliebiges recover-Guard anlegen `N.create_guard({source_type:"state_based", name:"PersAuto", health:{entity_id:"input_boolean.test_2"}, mode:"recover", strategy:"switch", switch_entity:"switch.test_template_switch", behavior:{debounce:3,cooldown:5}})` → `N.call("switch","turn_off",entity_id="switch.persauto_auto_reparatur")` → `N.st("switch.persauto_auto_reparatur")["state"]=="off"` → `N.wait(7)` (Flush) → RESTART.
  - **Assert:** Nach Restart `N.st("switch.persauto_auto_reparatur")["state"] == "off"`.
  - **Cleanup:** Subentry `"PersAuto"` löschen.

- [ ] **PERS-5 — Transiente States NICHT restored** · `P0`
  - **Prüft:** Nur `ESCALATED` wird aus dem Store wiederhergestellt; SUSPECT/RECOVERING/VERIFY/COOLDOWN werden verworfen und der State aus Live-Health neu abgeleitet (core/engine.py `_apply_persisted` — `if data.get("state") == GState.ESCALATED.value`; sonst Default `OK` + erstes `_evaluate` in `async_start`).
  - **Files:** core/engine.py `_apply_persisted` (nur ESCALATED-Branch) + `async_start` (`self._evaluate()`).
  - **Treiber:** Guard in COOLDOWN bringen: PERS-3-Guard reicht; direkt nach erfolgreichem Recover ist es `cooldown`. `N.guard("persstat")[0]=="cooldown"` prüfen → SOFORT `N.wait(7)` (Flush schreibt `state:"cooldown"` in den Store) → RESTART während Health gesund (test_5 = on).
  - **Assert:** Im rohen Store steht zwar evtl. `cooldown`, aber nach Restart `N.guard("persstat")[0] == "ok"` (transienter State verworfen, Live-Health=OK). Gegenprobe Marker: `N.log()` zeigt direkt nach Restart KEIN `"PersStat recovered after"` (kein neuer Recover-Lauf).
  - **Cleanup:** wie PERS-3.

- [ ] **PERS-6 — Store-Flush beim Reload/Unload (kein stale Store)** · `P0`
  - **Prüft:** `async_unload_entry` schreibt den Snapshot synchron vor dem Teardown (__init__.py), damit ein sofortiger Reload (Reconfigure/Add-Port) keinen veralteten Store liest.
  - **Files:** __init__.py `async_unload_entry` (`store.async_save(serialize())` VOR `async_unload_platforms`/`engine.async_stop()`).
  - **Treiber:** PERS-3-Guard nach Recover (`recover_count==1`, COOLDOWN) → OHNE `N.wait(7)` einen Entry-Reload erzwingen über die Options/Add-Port (der Options-Update-Listener `_async_reload_entry` reloadet den Entry): `N.add_port({label:"flushport", actuator:"switch.test_template_switch", id_static:"flush:aa:bb", status_entity:"binary_sensor.test_reachable"})` → direkt `N.guard("persstat")[1]["recover_count"]`.
  - **Assert:** `recover_count == 1` direkt nach dem Reload (nicht 0) → Unload hat synchron geflusht.
  - **Cleanup:** Subentry `"PersStat"` löschen + `N.remove_port("flushport")`.

### P0 — Health-Robustheit (event-getrieben)

> Rename/Removal/Disabled werden über `async_track_entity_registry_updated_event` gefangen (core/engine.py `_handle_registry_event`). Setup-Validierung: `__init__` plant `_check_config` je Engine via `async_at_started` **nach** `async_forward_entry_setups` (eigene View-Entities sind dann registriert → Self-Reference-Check greift auch beim Laufzeit-Anlegen).
> Marker sind ENGLISCH (Logs immer englisch). Registry-Mutationen brauchen den WS-/registry-Pfad; `N.setstate` allein triggert KEIN Registry-Event. Der Testkit hat `N.ws(commands)` für direkte WS-Aufrufe (`config/entity_registry/{remove,update}`).
> Hinweis: HR-2..5 mutieren die Entity-Registry. Verwende eine WEGWERF-Entität (z. B. einen extra angelegten `input_boolean`-Helfer), nicht die geteilten `input_boolean.test_*` (die andere Tests brauchen). Ist kein Wegwerf-Helfer verfügbar, bestätige NUR Marker + Codepfad (Files) und markiere den Live-Schritt als manuell.

- [ ] **HR-1 — Setup: fehlende Health-Entität → ERROR `does not exist`** · `P0`
  - **Prüft:** Ein Guard auf eine nicht existierende Entität loggt beim Start einen Config-Error (core/engine.py `_check_config`).
  - **Files:** core/engine.py `_check_config` — `LOGGER.error("%s: health entity %s does not exist", ...)`.
  - **Treiber:** `N.create_guard({source_type:"state_based", name:"HrMissing", health:{entity_id:"binary_sensor.does_not_exist_xyz"}, mode:"recover", strategy:"switch", switch_entity:"switch.test_template_switch", behavior:{debounce:3,cooldown:5}})` → `N.wait(3)` → `N.log()`.
  - **Assert:** `N.log()` enthält `"HrMissing: health entity binary_sensor.does_not_exist_xyz does not exist"`.
  - **Cleanup:** Subentry `"HrMissing"` löschen.

- [ ] **HR-2 — Live-Remove der Health-Entität → ERROR `was removed`** · `P0`
  - **Prüft:** Wird die überwachte Entität zur Laufzeit aus der Registry gelöscht, loggt das Guard `was removed` (core/engine.py action `remove`).
  - **Files:** core/engine.py `_handle_registry_event` — `LOGGER.error("%s: health entity %s was removed", ...)`.
  - **Treiber:** Wegwerf-Helfer anlegen (UI/WS), Guard darauf legen → entity_registry_id ermitteln und Remove feuern: `N.ws([{"type":"config/entity_registry/remove","entity_id":"<eid>"}])` → `N.wait(3)` → `N.log()`. Kein Wegwerf-Helfer → nur Marker-/Codepfad bestätigen.
  - **Assert:** `N.log()` enthält `"<Name>: health entity <eid> was removed"` nach dem Remove-Event.
  - **Cleanup:** Subentry löschen.

- [ ] **HR-3 — Disabled → ERROR `is disabled — guard is blind`** · `P0`
  - **Prüft:** Wird die Health-Entität deaktiviert, loggt das Guard sowohl beim Setup (`_check_config`) als auch live (Registry-Event) einen ERROR.
  - **Files:** core/engine.py (Setup-Pfad) + (Live-`disabled_by`-Pfad), Marker `"is disabled — guard is blind"`.
  - **Treiber:** Guard auf Wegwerf-Helfer → live deaktivieren: `N.ws([{"type":"config/entity_registry/update","entity_id":"<eid>","disabled_by":"user"}])` → `N.wait(3)` → `N.log()`. Kein Wegwerf-Helfer → Marker-/Codepfad bestätigen.
  - **Assert:** `N.log()` enthält `"<Name>: health entity <eid> is disabled — guard is blind"`.
  - **Cleanup:** Entität re-enablen (`disabled_by:null`) + Subentry löschen.

- [ ] **HR-4 — Re-enabled → INFO `re-enabled`** · `P0`
  - **Prüft:** Wird eine zuvor deaktivierte Health-Entität wieder aktiviert, loggt das Guard `re-enabled` (core/engine.py).
  - **Files:** core/engine.py `_handle_registry_event` — `else: LOGGER.info("%s: health entity %s re-enabled", ...)`.
  - **Treiber:** Anschluss an HR-3: `N.ws([{"type":"config/entity_registry/update","entity_id":"<eid>","disabled_by":None}])` → `N.wait(3)` → `N.log()`.
  - **Assert:** `N.log()` enthält `"<Name>: health entity <eid> re-enabled"`.
  - **Cleanup:** Subentry löschen.

- [ ] **HR-5 — Rename-Following: Config-`entity_id` wird aktualisiert** · `P0`
  - **Prüft:** Wird die Health-Entität umbenannt, loggt das Guard INFO `renamed old -> new` und der `_rename_handler` schreibt die neue id flach in `data.health.entity_id` der Subentry (core/engine.py + __init__.py `_rename_handler` → Reload, watcht neue id).
  - **Files:** core/engine.py (`LOGGER.info("Health entity for %s renamed %s -> %s", ...)`) + __init__.py `_rename_handler` (schreibt `CONF_ENTITY_ID` in die flache `health`-Dict). Hinweis: gespeicherte Health-Config ist FLACH (`data["health"]["entity_id"]`), nicht unter `state_check`.
  - **Treiber:** Guard auf Wegwerf-Helfer → umbenennen: `N.ws([{"type":"config/entity_registry/update","entity_id":"<old>","new_entity_id":"<old>_renamed"}])` → `N.wait(3)` → `N.log()` + `N.list_subentries(N.hub_id())` (Subentry-`data.health.entity_id` muss neue id sein).
  - **Assert:** `N.log()` enthält `"Health entity for <Name> renamed <old> -> <new>"` UND die Subentry-Daten tragen die neue `entity_id` unter `data.health.entity_id`.
  - **Cleanup:** Entität zurückbenennen + Subentry löschen.

- [ ] **HR-6 — Startup-Erkennung „off": beim Hochlauf schon unhealthy** · `P0`
  - **Prüft:** Ist die Health beim Start bereits unhealthy, erkennt das erste `_evaluate` in `async_start` das und geht in SUSPECT (core/engine.py `async_start` + `_evaluate` → `_enter_suspect`).
  - **Files:** core/engine.py `async_start` (`self._evaluate()`) + `_evaluate` + `_enter_suspect`.
  - **Treiber:** `N.create_guard({source_type:"state_based", name:"HrBoot", health:{entity_id:"input_boolean.test_3", on_value:["on"], off_value:["off"]}, mode:"recover", strategy:"action_check", action:[{"service":"input_text.set_value","data":{"entity_id":"input_text.test_note","value":"x"}}], behavior:{debounce:20,cooldown:5,boot_window:4,max_attempts:1}})` → `N.call("input_boolean","turn_off",entity_id="input_boolean.test_3")` → RESTART (Health bleibt off) → nach RUNNING SCHNELL `N.guard("hrboot")`.
  - **Assert:** `N.guard("hrboot")[0] == "suspect"` kurz nach Restart (langes debounce=20s hält SUSPECT pollbar); im `N.log()` `"HrBoot unhealthy, waiting 20s (debounce)"`.
  - **Cleanup:** Subentry `"HrBoot"` + `N.call("input_boolean","turn_on",entity_id="input_boolean.test_3")`.

### P0 — Config-Error-Logging (System-Log ERROR, kein Notify)

> Driver-Config-Fehler kommen aus `driver.config_errors()` und werden in `_check_config` als `"<Name>: <err>"` geloggt — die Validierung plant `__init__` via `async_at_started` nach dem Platform-Setup, also wenn HA RUNNING ist und die eigenen Entities existieren.

- [ ] **CFG-1 — Valide Config → 0 Necromancer-ERRORs** · `P0`
  - **Prüft:** Bei ausschließlich validen Guards stehen keine necromancer-Config-Errors im Log.
  - **Treiber:** Eine saubere Guard-Garnitur sicherstellen (alle Health-/Switch-/Port-Refs existieren) → RESTART → nach RUNNING `N.log()`.
  - **Assert:** `N.log()` enthält KEINE Zeile mit `"does not exist"`, `"not found"`, `"no ports configured"` oder `"is disabled — guard is blind"` für ein necromancer-Guard. (Grep auf diese vier Marker = leer.)
  - **Cleanup:** —

- [ ] **CFG-2 — Switch fehlt (Setup) → ERROR `switch entity X not found`** · `P0`
  - **Prüft:** Ein `switch`/`switch_check`-Guard auf eine nicht existierende Switch-Entität loggt beim Start einen Config-Error (core/drivers/switch_cycle.py `config_errors`).
  - **Files:** core/drivers/switch_cycle.py — `return [f"switch entity {self.switch_entity} not found"]`; geloggt in core/engine.py.
  - **Treiber:** `N.create_guard({source_type:"state_based", name:"CfgSw", health:{entity_id:"input_boolean.test_4"}, mode:"recover", strategy:"switch", switch_entity:"switch.does_not_exist_xyz", behavior:{debounce:3,cooldown:5}})` → `N.wait(3)` → `N.log()`.
  - **Assert:** `N.log()` enthält `"CfgSw: switch entity switch.does_not_exist_xyz not found"`.
  - **Cleanup:** Subentry `"CfgSw"` löschen.

- [ ] **CFG-3 — poe_port ohne Ports → ERROR `no ports configured`** · `P0`
  - **Prüft:** Ein `poe_port`-Guard loggt einen Config-Error, wenn die Fabric keine Ports kennt (core/drivers/poe_port.py `config_errors` → `fabric.port_count == 0`).
  - **Files:** core/drivers/poe_port.py — Marker `"no ports configured — add ports in the integration's options"`.
  - **Treiber:** Sicherstellen, dass KEINE Ports konfiguriert sind (sonst zuerst alle via `N.remove_port(label)` entfernen) → `N.create_guard({source_type:"state_based", name:"CfgPoe", health:{entity_id:"input_boolean.sim_device_power"}, mode:"recover", strategy:"poe_port", expected_id:"aa:bb:cc:dd:ee:ff", behavior:{debounce:3,cooldown:5,boot_window:4,max_attempts:2}})` → `N.wait(3)` → `N.log()`.
  - **Assert:** `N.log()` enthält `"CfgPoe: poe_port 'aa:bb:cc:dd:ee:ff': no ports configured"`.
  - **Cleanup:** Subentry `"CfgPoe"` löschen.

- [ ] **CFG-4 — Action-Service fehlt: Laufzeit-ERROR, kein Pre-Check** · `P0`
  - **Prüft:** Für action/actions-Strategien gibt es bewusst keinen `can_recover`-Pre-Check; ein fehlender Service wirft erst beim Recover und wird als `Recovery driver failed` geloggt → retry/escalate, KEIN falscher Erfolg (core/engine.py `_run_recovery_cycle`).
  - **Files:** core/engine.py — `except Exception … LOGGER.exception("Recovery driver failed for %s", self.name)`; KEIN `config_errors` für action-Driver.
  - **Treiber:** `N.create_guard({source_type:"state_based", name:"CfgAct", health:{entity_id:"input_boolean.test_6", on_value:["on"], off_value:["off"]}, mode:"recover", strategy:"action_check", action:[{"service":"nonexistent.service","data":{}}], behavior:{debounce:3,cooldown:5,boot_window:3,max_attempts:2}})` → `N.call("input_boolean","turn_off",entity_id="input_boolean.test_6")` → `N.wait(14)` → `N.guard("cfgact")` + `N.log()`.
  - **Assert:** `N.log()` enthält `"Recovery driver failed for CfgAct"` UND `N.guard("cfgact")[0] == "escalated"` (max_attempts erreicht), NICHT `cooldown`/`ok`.
  - **Cleanup:** Subentry `"CfgAct"` + `N.call("input_boolean","turn_on",entity_id="input_boolean.test_6")`.

- [ ] **CFG-5 — F2 Feedback-Loop-WARNING (Template referenziert eigene Entity)** · `P1`
  - **Prüft:** Ein Template-Health, das eine der EIGENEN Guard-Entities referenziert, erzeugt eine Feedback-Loop-WARNING — jetzt **auch beim Anlegen zur Laufzeit** (die Validierung wird in `__init__` NACH `async_forward_entry_setups` via `async_at_started` geplant → eigene Entities sind registriert), kein Crash. Referenzen auf **fremde** Guards lösen KEINE Warnung aus (Supervisor-Guards sind gewollt).
  - **Files:** core/engine.py — `own.intersection(self.health.referenced_entities())` → `LOGGER.warning("%s: health references its own entit(ies) %s — feedback loop; …")`.
  - **Treiber:** `N.create_guard({source_type:"template_based", name:"CfgLoop", health:{template:"{{ is_state('sensor.cfgloop_status','ok') }}"}, mode:"recover", strategy:"action_check", action:[{"service":"input_text.set_value","data":{"entity_id":"input_text.test_note","value":"x"}}], behavior:{debounce:3,cooldown:5,boot_window:3,max_attempts:1}})` → erster Load: KEINE Warnung erwartet → RESTART (oder Reload) → `N.wait(3)` → `N.log()`.
  - **Assert:** Nach Reload `N.log()` enthält `"CfgLoop: health references its own entit(ies)"` und `"feedback loop"`; HA bleibt RUNNING (`N.g("/api/config")["state"]=="RUNNING"`), 0 Tracebacks.
  - **Cleanup:** Subentry `"CfgLoop"` löschen.

- [ ] **CFG-6 (B3) — Template-Health blind: fehlende/disabled referenzierte Entity** · `P1`
  - **Prüft:** `_check_config` validiert bei tracking-Sources (template) nicht nur `watched_entities` (= leer), sondern die tatsächlich gelesenen `referenced_entities()`. Eine einzelne fehlende/disabled Entity → **WARNING** (named); sind ALLE referenzierten Entities weg → **ERROR `guard is blind`** (state_based meldet das längst, template war bis B3 still blind).
  - **Files:** core/engine.py — `if not self.health.watched_entities:` → pro Entity `LOGGER.warning("%s: health template references %s, which does not exist"/"which is disabled")`; bei `len(blind)==len(referenced)` zusätzlich `LOGGER.error("%s: health template reads only missing/disabled entities %s — guard is blind")`. Hängt an `referenced_entities()` (template.py = `async_render_to_info().entities`) — beachtet daher nur Entities, die beim Rendern wirklich gelesen werden (Jinja-Kurzschluss bei `or` lässt die zweite Seite aus).
  - **Treiber:** Voll blind: `N.create_guard({source_type:"template_based", name:"CfgBlind", health:{template:"{{ is_state('binary_sensor.ghost_xyz','on') }}"}, mode:"notify", behavior:{debounce:3}})` → RESTART → `N.wait(3)` → `N.log()`. Teilweise (1 von 2 fehlt, kein false-blind): Template `"{{ is_state('<lebende Entity>','on') and states('binary_sensor.ghost_xyz') != 'never' }}"`.
  - **Assert:** Voll blind: `N.log()` enthält `"CfgBlind: health template reads only missing/disabled entities"` + `"guard is blind"` + `"binary_sensor.ghost_xyz"`. Teilweise: `"does not exist"` für die fehlende Entity, aber **kein** `"guard is blind"`. 0 Tracebacks.
  - **Automatisiert:** `test_integration.py::test_health_template_all_missing_is_blind` (ERROR-Pfad) + `::test_health_template_partial_missing_warns_only` (nur WARNING, kein false-blind).
  - **Cleanup:** Subentry `"CfgBlind"` löschen.

### Hinweis zum doc-internen Zählerstand

- [ ] **DOC-1 — Suite-Zählerstand im Regressions-Doc aktualisieren** · `P2`
  - **Prüft:** Der Header von REGRESSION.md nennt veraltete Testzahlen und veraltete „lock"-Formulierung.
  - **Files:** REGRESSION.md Header („51 automatisierte Tests grün") + die Suite-Aufschlüsselungszeile („test_units (18) · test_poe (15) · test_engine (10) · test_integration (8) = 51 grün") + die Zeile mit dem Wort „lock". Aktuell: `test_units=29 · test_poe=16 · test_engine=34 · test_integration=12-checks (7 Funktionen)` → Summe 91; PoE-Per-Port-`Lock` wurde ENTFERNT → durch **Coalescing** (`_inflight`-Task + `asyncio.shield`) ersetzt → „lock/Platzhalter" auf „Coalescing/Platzhalter" umtexten.
  - **Assert:** Header-/Suite-Aufschlüsselungszeilen auf die aktuellen Zahlen korrigiert (29/16/34/12-checks = 91); die „lock"-Zeile ersetzt „lock" durch „coalescing".
  - **Cleanup:** —

---

## Device-Link-Namen · State-Machine · Notify-i18n · Config-Flow/Reload · Kosmetik

### P0 — Device-Link-Namenslogik

> Hinweis: DLN1/DLN2 prüfen `LOGGER.debug`-Marker. Diese erscheinen in `N.log()` nur,
> weil die Dev-Config `custom_components.necromancer: debug` setzt (verifiziert in
> `config/configuration.yaml`). Bei abweichender Log-Stufe sind die Marker nicht sichtbar.

- [ ] **DLN1 — Verknüpfen hängt 4 Entities ans Zielgerät** · `P0`
  - **Prüft:** Ein Guard mit zugewiesenem Gerät (`device_id`) erzeugt KEIN eigenes Standalone-Gerät, sondern hängt seine 4 Entities unter dem Subentry an das gewählte Zielgerät; dessen Name bleibt unangetastet.
  - **Files:** `__init__.py` → `_reconcile_devices` (`standalone`/`linked_targets`-Split, stale-device-Remove `"Removing stale guard device %s"`); `config_flow_helpers/schemas.py` → `_device_schema` (flaches Feld `CONF_DEVICE_ID="device_id"` als `DeviceSelector()`, KEINE Section `assigned_device` mehr).
  - **Treiber:** Ziel-Device-id (`<tgt>`) aus `N.ws([{"type":"config/device_registry/list"}])` (irgendein Nicht-Necromancer-Gerät) holen. `N.create_guard` setzt kein Gerät → ein verlinkter Guard ist NICHT direkt über `create_guard` baubar; stattdessen Subentry-Flow manuell treiben: `N._post_flow(fid,{"source_type":"state_based"})` → Device-Step FLACH mit `{"name":"LinkTgtX","device_id":<tgt>,"entity_id":...,"on_value":[...],"off_value":[...]}` posten (kein `mode`-Feld, `device_id` top-level) → `N._post_flow(fid,{"strategy":"action_check"})` → Recover-Step **inkl. `"reload":{}`** posten (bei zugewiesenem Gerät ist die Reload-Section pflicht: `{"action":[...],"behavior":{...},"notification":{},"linked_guards":{},"reload":{}}`). Nach Reload (`POST .../entry/<hub>/reload`) Entity-Registry via WS lesen, nach `config_subentry_id==<sid>` filtern.
  - **Assert:** Zielgerät-Name unverändert; ≥4 Entities mit `config_subentry_id==<sid>`, **alle** mit `device_id==<tgt>`; eine davon ist der Status-Sensor (`*_status`). **Hinweis:** Bei Geräte-Link übernehmen die View-Entities den **Zielgeräte-Namen** (z. B. `sensor.<zielgerät>_status`), NICHT den Guard-Namen — also nicht auf `sensor.linktgtx_status` prüfen, sondern über `config_subentry_id` filtern. KEIN zusätzliches Device mit identifier `(necromancer,<sid>)` im Registry; bei vorher existierendem Standalone erscheint `"Removing stale guard device"` in `N.log()`.
  - **Cleanup:** `N.delete_subentry(eid, sid)`

- [ ] **DLN2 — Auflösen setzt Device-Namen auf Guard-Namen (kein name_by_user-Override)** · `P0`
  - **Prüft:** Reconfigure von „Gerät zugewiesen" → „kein Gerät" flaggt `name_reset`; nach Reload trägt das wiederhergestellte Standalone-Device den Guard-Namen, `name_by_user=None`.
  - **Files:** `config_flow.py` → `_finish` (`name_reset`-Set nur auf der Unlink-Transition); `__init__.py` → `_reconcile_devices` (`dev_reg.async_update_device(..., name=engine.name, name_by_user=None)`).
  - **Treiber:** Guard mit zugewiesenem Gerät anlegen (s. DLN1), dann Reconfigure-Flow ohne Gerät (`device_id` leer) durchlaufen. `N.wait(3)`; `N.log()`.
  - **Assert:** `N.log()` enthält `"Resetting device name to <name> after unlink"` (DEBUG, exakt: `"Resetting device name to %s after unlink"`); im `device_registry/list` hat das `(necromancer,<sid>)`-Device `name_by_user==None` und `name==<guard-name>`.
  - **Cleanup:** `N.delete_subentry(eid, sid)`

- [ ] **DLN3 — Guard-Rename ändert Device-Namen NICHT (kein falsches name_reset)** · `P0`
  - **Prüft:** Reine Umbenennung (Device blieb unverändert zugewiesen/standalone) löst KEIN `name_reset` aus — `_finish` flaggt nur, wenn vorher device_id gesetzt war und jetzt leer.
  - **Files:** `config_flow.py` → `_finish` (`if subentry.data.get(CONF_DEVICE_ID) and not data.get(CONF_DEVICE_ID)`).
  - **Treiber:** Standalone-Guard `eid,sid=N.create_guard({...,"name":"RenA",...})`. Reconfigure-Flow (Init: `POST .../subentries/flow` mit `{"handler":[hub,"device"],"subentry_id":sid}` → `step_id=="reconfigure"`) komplett re-driven, im Device-Step nur neuer Name `"RenB"`. `N.wait(3)`; `N.log()` + `device_registry/list`.
  - **Assert:** Log enthält NICHT `"Resetting device name to"` für diesen Guard; das Standalone-Device `(necromancer,<sid>)` heißt jetzt `name=="RenB"` (Geräte-Name folgt dem Guard-Namen), `name_by_user==None`. **Hinweis:** Die `entity_id` bleibt sticky (`sensor.rena_status`) — HA benennt entity_ids beim Geräte-Rename NICHT um; daher NICHT auf `sensor.renb_status` prüfen, sondern den Device-Namen bzw. die Existenz eines `*_status`-Sensors unter `config_subentry_id==<sid>`.
  - **Cleanup:** `N.delete_subentry(eid, sid)`

- [ ] **DLN4 — Self-/Cross-Link blockiert (`no_self_link`)** · `P0`
  - **Prüft:** Ein Necromancer-eigenes Gerät kann nicht als zugewiesenes Gerät (`device_id`) gewählt werden — Device-Step lehnt mit `no_self_link` ab.
  - **Files:** `config_flow.py` → `_is_own_device` + `async_step_device` (`errors[CONF_DEVICE_ID]="no_self_link"`); de.json `config_subentries.device.error.no_self_link`.
  - **Treiber:** Eigenes Guard-Device-id aus `device_registry/list` (identifier-domain `necromancer`) holen, Subentry-Flow bis Device-Step treiben und Device-Step FLACH mit `{"name":"SelfX","device_id":<own_id>,"entity_id":...,"on_value":[...],"off_value":[...]}` posten (kein `mode`-Feld, `device_id` top-level; der `no_self_link`-Fehler greift bereits im Device-Step, der Strategy-Step wird nie erreicht).
  - **Assert:** Antwort `step_id=="device"` mit `errors=={"device_id":"no_self_link"}` (kein `create_entry`).
  - **Cleanup:** Flow nicht abgeschlossen → „—"

- [ ] **DLN5 — device.id stabil über Link→Unlink→Rename** · `P1`
  - **Prüft:** Die Subentry-/Device-Identität `(necromancer,<sid>)` bleibt dieselbe über Link, Unlink und Rename hinweg (kein neues Device-Objekt).
  - **Files:** `__init__.py` → `_reconcile_devices` (identifier `(DOMAIN, subentry_id)` bleibt Schlüssel; Device wird per `dev_reg.async_get_device(identifiers={(DOMAIN, subentry_id)})` gefunden).
  - **Treiber:** Standalone-Guard anlegen → `<sid>` merken. Reconfigure mit zugewiesenem Gerät → reload. Reconfigure ohne → reload. Reconfigure Rename → reload. Jeweils `device_registry/list` nach `(necromancer,<sid>)` filtern.
  - **Assert:** `<sid>` (Subentry-id) identisch über alle Schritte; das Standalone-Device nach dem finalen Unlink trägt wieder denselben identifier `(necromancer,<sid>)`.
  - **Cleanup:** `N.delete_subentry(eid, sid)`

### P1 — State-Machine

- [ ] **SM1 — Happy Path OK→SUSPECT→RECOVERING→VERIFY→COOLDOWN→OK** · `P1`
  - **Prüft:** Voller Genesungszyklus mit `*_check`-Strategie; nach Erfolg `recover_count=1`.
  - **Files:** `core/engine.py` → `_evaluate`→`_enter_suspect`→`_debounce_done`→`_start_cycle`→`_run_recovery_cycle`→`_recover_success`.
  - **Treiber:** `N.call("input_boolean","turn_on","entity_id","input_boolean.test_5")`. `eid,sid=N.create_guard({"source_type":"state_based","name":"SMHappy","health":{"entity_id":"input_boolean.test_5","on_value":["on"],"off_value":["off"]},"mode":"recover","strategy":"action_check","action":[{"service":"input_boolean.turn_on","data":{"entity_id":"input_boolean.test_5"}}],"behavior":{"debounce":1,"cooldown":3,"boot_window":10,"max_attempts":2}})`. Health brechen: `N.call("input_boolean","turn_off","entity_id","input_boolean.test_5")`. `N.wait(2)` (SUSPECT/RECOVERING/VERIFY zu schnell → in Log). `N.wait(2)`; `N.guard("smhappy")`.
  - **Assert:** `N.log()` enthält `"SMHappy unhealthy, waiting"`, `"SMHappy debounce elapsed, starting recovery"`, `"SMHappy recovered after"`; nach Cooldown `N.guard("smhappy")[0]=="ok"` und `attrs["recover_count"]==1`.
  - **Cleanup:** `N.delete_subentry(eid, sid)`

- [ ] **SM2 — Manueller Recover via Button umgeht Debounce + Auto-Gate** · `P1`
  - **Prüft:** `button.<slug>_reparieren` ruft `async_manual_recover` → `attempt=0` + sofortiger `_start_cycle`, ohne Debounce und ohne Auto-aus-Gate; Doppelpress während Cycle ignoriert (Busy-Guard `_busy()`).
  - **Files:** `core/engine.py` → `async_manual_recover` (Busy-Guard `_busy()`); `button.py` → `RecoverButton.async_press`.
  - **Treiber:** Guard wie SM1 (`"name":"SMManual"`, großer `"debounce":600`). `N.call("input_boolean","turn_off","entity_id","input_boolean.test_5")`; `N.wait(1)` → bleibt SUSPECT (Debounce nicht abgelaufen). `N.call("button","press","entity_id","button.smmanual_reparieren")`. `N.wait(2)`; `N.guard("smmanual")`.
  - **Assert:** `N.log()` enthält `"SMManual manual recovery requested"` und `"SMManual recovered after"`; `N.guard("smmanual")[0]` in `("cooldown","ok")`.
  - **Cleanup:** `N.delete_subentry(eid, sid)`

- [ ] **SM3 — Auto aus → ESCALATED, kein Recover-Versuch** · `P1`
  - **Prüft:** Mit deaktiviertem Auto-Switch eskaliert der Guard bei kranker Health im Debounce sofort (Policy-Gate `should_attempt`→`auto_off`), startet KEINEN Cycle, Notify `no_auto_recovery`.
  - **Files:** `core/engine.py` → `_debounce_done`; `core/policies/base.py` → `should_attempt` (`return False, REASON_AUTO_OFF`).
  - **Treiber:** Guard wie SM1 (`"name":"SMAutoOff"`, `"debounce":1`). `N.call("switch","turn_off","entity_id","switch.smautooff_auto_reparatur")`; `N.wait(1)`. `N.call("input_boolean","turn_off","entity_id","input_boolean.test_5")`; `N.wait(3)`; `N.guard("smautooff")`.
  - **Assert:** `N.guard("smautooff")[0]=="escalated"`; `attrs["recover_count"]==0`; `N.log()` enthält `"SMAutoOff still unhealthy but auto-recovery is off (auto_off)"`; KEIN `"recovery attempt"` für SMAutoOff.
  - **Cleanup:** `N.delete_subentry(eid, sid)`

- [ ] **SM4 — Max-Attempts → ESCALATED (Verify-Timeout, kein Traceback)** · `P1`
  - **Prüft:** Aktion heilt die Health NICHT → VERIFY läuft je Versuch ab → Retry bis `max_attempts` → `escalated`, `attempt==max`, `recover_count==0`; terminaler ERROR ohne Traceback (`_escalate` statt `LOGGER.exception`).
  - **Files:** `core/engine.py` → `_run_recovery_cycle` (`if self.attempt>=self.max_attempts: self._escalate()`), `_wait_health_ok` `_escalate`.
  - **Treiber:** Guard `"name":"SMMax"`, `"strategy":"action_check"`, Aktion schreibt NUR Notiz (heilt nicht): `"action":[{"service":"input_text.set_value","data":{"entity_id":"input_text.test_note","value":"poke"}}]`, `"behavior":{"debounce":1,"cooldown":3,"boot_window":2,"max_attempts":2}`. `N.call("input_boolean","turn_off","entity_id","input_boolean.test_5")`; `N.wait(8)`; `N.guard("smmax")`.
  - **Assert:** `N.guard("smmax")[0]=="escalated"`; `attrs["attempt"]==2`, `attrs["recover_count"]==0`; `N.log()` enthält `"SMMax could not be recovered after 2 attempt(s)"`; KEIN `"Traceback"` rund um SMMax.
  - **Cleanup:** `N.delete_subentry(eid, sid)`

- [ ] **SM5 — COOLDOWN→SUSPECT (in Cooldown wieder krank)** · `P1`
  - **Prüft:** Wird die Health während COOLDOWN erneut unhealthy, geht der Guard über `_cooldown_done` zurück in SUSPECT (nicht direkt OK).
  - **Files:** `core/engine.py` → `_cooldown_done` (`if self.health.evaluate()==Health.UNHEALTHY: self._enter_suspect()`).
  - **Treiber:** Guard wie SM1 (`"name":"SMCool"`, `"cooldown":6`, `"debounce":1`). Health brechen → heilen lassen (Aktion `input_boolean.turn_on test_5`) → in COOLDOWN erneut `N.call("input_boolean","turn_off","entity_id","input_boolean.test_5")`. `N.guard("smcool")` mehrfach pollen (COOLDOWN/SUSPECT sind langsam genug).
  - **Assert:** Beobachtete Folge in `N.guard`/`N.log`: `cooldown` → erneut `"SMCool unhealthy, waiting"` (SUSPECT) statt direktem `ok`.
  - **Cleanup:** `N.delete_subentry(eid, sid)`

- [ ] **SM6 — actions-Strategie: Aus-Aktion → Delay → Ein-Aktion** · `P1`
  - **Prüft:** `actions`-Strategie (ohne `_check`) führt getrennte off- und on-Sequenzen mit `off_on_delay` aus (Driver `action_cycle`); ohne Check sofortiger `_recover_success`.
  - **Treiber:** Guard `"source_type":"state_based","name":"SMActions"`, `"health":{"entity_id":"input_boolean.test_5","on_value":["on"],"off_value":["off"]}`, `"mode":"recover"`, `"strategy":"actions"`, `"off_action":[{"service":"input_boolean.turn_off","data":{"entity_id":"input_boolean.test_6"}}]`, `"on_action":[{"service":"input_boolean.turn_on","data":{"entity_id":"input_boolean.test_6"}}]`, `"off_on_delay":2`, `"behavior":{"debounce":1,"cooldown":3}` (ohne `_check` → kein boot_window/max_attempts). Health (`input_boolean.test_5`) brechen; `N.wait(5)`; `N.st("input_boolean.test_6")`.
  - **Assert:** `N.log()` enthält `"SMActions recovery attempt 1"`; `input_boolean.test_6` endet `state=="on"` (Ein-Aktion lief nach off+delay).
  - **Cleanup:** `N.delete_subentry(eid, sid)`

### P1 — Notify (i18n)

> Hinweis: Die de-/en-Texte stehen in `const.py` `NOTIFY_MESSAGES` und werden per
> `str.format` mit `{name}/{attempt}/{max}` gerendert. Die User-Notify-Aktion erhält
> `message/name/event/...` als Script-Variablen → in der Aktion `{{ message }}` (Jinja).

- [ ] **NOT1 — Logs Englisch, Notify-Meldung Deutsch (language=de)** · `P1`
  - **Prüft:** Bei `hass.config.language=="de"` rendert `async_notify` die de-Templates aus `NOTIFY_MESSAGES`, während die Log-Zeilen englisch bleiben.
  - **Files:** `core/notify.py` → `async_notify` (`lang=...`) + `_resolve` (`messages=NOTIFY_MESSAGES.get(lang,...)`); `const.py` → `NOTIFY_MESSAGES["de"]`.
  - **Treiber:** Guard `"name":"NotiDe"`, `"mode":"recover"`, `"strategy":"action_check"` (heilt nicht: Aktion schreibt `input_text.test_note`), `"notify_action":[{"service":"input_text.set_value","data":{"entity_id":"input_text.test_note","value":"{{ message }}"}}]`, `"behavior":{"debounce":1,"cooldown":3,"boot_window":2,"max_attempts":1}`. Health brechen; `N.wait(6)`; `N.st("input_text.test_note")`.
  - **Assert:** `N.st("input_text.test_note")["state"]` enthält deutschen Text — final (max_attempts=1, escaliert) `"Reparatur fehlgeschlagen nach 1 Versuch."` (plural-korrekt), zwischenzeitlich auch `"Reparaturversuch 1 von 1."` möglich; `N.log()` bleibt englisch (`"recovery attempt"`/`"could not be recovered"`).
  - **Cleanup:** `N.delete_subentry(eid, sid)`

- [ ] **NOT2 — Notify-Aktion mit defektem Service → gefangen, kein Crash** · `P1`
  - **Prüft:** Eine Notify-Aktion mit nicht existierendem Service wird abgefangen (`Notify action failed`/`Notify action invalid`), der Guard läuft normal weiter (Notify detached via `hass.async_create_task`).
  - **Files:** `core/notify.py` → `_run` (`except vol.Invalid` → `"Notify action invalid for %s"` / `except Exception` → `"Notify action failed for %s"`).
  - **Treiber:** Guard `"name":"NotiBad"`, `"notify_action":[{"service":"notify.does_not_exist","data":{"message":"{{ message }}"}}]`, `"strategy":"action_check"` (heilt: `input_boolean.turn_on test_5`), `"behavior":{"debounce":1,"cooldown":3,"boot_window":10,"max_attempts":2}`. Health brechen; `N.wait(5)`; `N.guard("notibad")`.
  - **Assert:** `N.log()` enthält `"Notify action"` (failed oder invalid) für NotiBad; Guard erreicht trotzdem `cooldown`/`ok` (`N.guard("notibad")[0]` in `("cooldown","ok")`); HA bleibt RUNNING (`N.g("/api/config")["state"]=="RUNNING"`).
  - **Cleanup:** `N.delete_subentry(eid, sid)`

- [ ] **NOT3 — Blockiert/Auto-aus = EINE Meldung (kein Doppel-Notify)** · `P1`
  - **Prüft:** Bei Auto-aus-Eskalation feuert genau EIN `no_auto_recovery`-Notify (über `_debounce_done`-Pfad), kein zusätzliches `recovery_attempt`.
  - **Files:** `core/engine.py` → `_debounce_done` (genau ein `_notify("no_auto_recovery", reason=reason)`, dann `_set_state(GState.ESCALATED)`).
  - **Treiber:** Guard wie SM3, `notify_action` schreibt `{{ event }}` an `input_text.test_note`: `"notify_action":[{"service":"input_text.set_value","data":{"entity_id":"input_text.test_note","value":"{{ event }}"}}]`. Auto aus, Health brechen; `N.wait(4)`; `N.st("input_text.test_note")`; `N.log()`.
  - **Assert:** Notiz endet `=="no_auto_recovery"`; in `N.log()` für diesen Guard KEIN `"recovery attempt"` (Cycle nie gestartet).
  - **Cleanup:** `N.delete_subentry(eid, sid)`

- [ ] **NOT4 — Variablen `{{ message }}`/`{{ name }}`/`{{ event_text }}`/`{{ event }}` in der Aktion** · `P1`
  - **Prüft:** Die Notify-Aktion erhält die Variablen `message` (fertig lokalisiert), `name`, `event` plus Event-Params (attempt/max).
  - **Files:** `core/notify.py` → `variables={"message","name","event_text","event",**params}` + `actions.async_run` (Script-Variablen).
  - **Treiber:** Guard `"name":"NotiVars"`, `"notify_action":[{"service":"input_text.set_value","data":{"entity_id":"input_text.test_note","value":"{{ name }}|{{ event }}|{{ message }}"}}]`, heilbar (`action_check`, `input_boolean.turn_on test_5`), `"behavior":{"debounce":1,"cooldown":3,"boot_window":10,"max_attempts":2}`. Health brechen; `N.wait(4)`; `N.st("input_text.test_note")`.
  - **Assert:** Notiz beginnt mit `"NotiVars|"`; finaler `event` ist `recovery_success` (Heilung gelingt) → `"NotiVars|recovery_success|"`, danach der lokalisierte de-`message`-Text (`"Reparatur erfolgreich."`). (Zwischenzeitlich `recovery_attempt` möglich, wird aber überschrieben.)
  - **Cleanup:** `N.delete_subentry(eid, sid)`

### P1 — Config-Flow / Reload

- [ ] **CF1 — Hub: zweite Instanz → already_configured** · `P1`
  - **Prüft:** Der Service-Entry ist Singleton; ein zweiter User-Flow bricht mit `already_configured` ab.
  - **Files:** `config_flow.py` → `async_step_user` (`if self._async_current_entries(): async_abort("already_configured")`); de.json `config.abort.already_configured`.
  - **Treiber:** Hub existiert bereits (`N.hub_id()`). Neuen User-Flow starten: `requests.post(N.BASE+"/api/config/config_entries/flow", headers=N.H, json={"handler":"necromancer"}).json()`.
  - **Assert:** Antwort enthält `"type":"abort"` und `"reason":"already_configured"`.
  - **Cleanup:** „—"

- [ ] **CF2 — Gerät hinzufügen/Reconfigure/Entfernen → Auto-Reload, andere Guards unberührt** · `P1`
  - **Prüft:** Subentry-Änderungen lösen `_async_reload_entry` aus; ein paralleler, unveränderter Guard behält Status/Stats (Store-Flush bei Unload).
  - **Files:** `__init__.py` → `_async_reload_entry` (`async_reload`); `async_unload_entry` Store-Flush (`store.async_save(serialize())`).
  - **Treiber:** Bystander-Guard `eidA,sidA=N.create_guard({...,"name":"CFBy",...})`; recover_count via Heilzyklus auf 1 bringen, in `ok` ruhen lassen. Dann zweiten Guard `eidB,sidB=N.create_guard({...,"name":"CFNew",...})` anlegen (löst Reload). `N.wait(3)`; `N.guard("cfby")`.
  - **Assert:** `N.guard("cfby")[0]=="ok"` und `attrs["recover_count"]==1` (überlebt Reload); `N.log()` ohne neue Errors für CFBy.
  - **Cleanup:** `N.delete_subentry(eidA,sidA)`; `N.delete_subentry(eidB,sidB)`

- [ ] **CF3 — Reconfigure-Defaults korrekt vorbefüllt** · `P1`
  - **Prüft:** Der Reconfigure-Flow lädt Source-Type, Name, Entität/Template, on/off-Listen, Strategie, Verhalten, Notify-Aktion und device_id korrekt vor.
  - **Files:** `config_flow_helpers/schemas.py` → `_source_type_of`, `_health_defaults` (inkl. `CONF_DEVICE_ID`), `_current_strategy` (liest `CONF_HEALTH_CHECK`), `_behavior_defaults`/`_switch_defaults`/`_action_defaults`.
  - **Treiber:** Guard `eid,sid=N.create_guard({"source_type":"state_based","name":"CFRe","mode":"recover","health":{"entity_id":"input_boolean.test_5"},"strategy":"switch_check","switch_entity":"switch.test_template_switch","behavior":{"debounce":1,"cooldown":3,"boot_window":10,"max_attempts":2}})`. Reconfigure-Subentry-Flow starten (`/api/config/config_entries/subentries/flow` mit `{"handler":[hub,"device"],"subentry_id":sid}`), die Steps NICHT submitten, sondern `data_schema`-Defaults inspizieren.
  - **Assert:** Source-Step Default == `state_based`; Device-Step suggested `name=="CFRe"`, on=`["on"]`/off=`["off"]`; Strategy-Step Default == `switch` (Basis-Strategie via `_current_strategy`; der Health-Check ist nicht Teil der Strategie); Switch-Step suggested `switch_entity=="switch.test_template_switch"`; Behavior-Section: `health_check`-Toggle vorbelegt `true` (gespeichert), Behavior-Werte == die gesetzten.
  - **Cleanup:** `N.delete_subentry(eid,sid)`

- [ ] **CF4 — Reconfigure Source-Wechsel state↔template** · `P1`
  - **Prüft:** Source-Step-Default folgt `_source_type_of`; nach Wechsel zeigt der Device-Step die passenden flachen Felder (state: `entity_id`/`on_value`/`off_value` ↔ template: `template`) und speichert die neue Source.
  - **Files:** `config_flow.py` → `_source` (`default=_source_type_of(...)`), `async_step_device` (`source_type=self._source_type` an `_device_schema`).
  - **Treiber:** state-Guard `eid,sid=N.create_guard({...,"name":"CFSrc",...})` anlegen. Reconfigure: Source-Step mit `{"source_type":"template_based"}` posten → Device-Step-Schema prüfen. Template `{{ is_state('input_boolean.test_5','on') }}` setzen, abschließen, reload.
  - **Assert:** Reconfigure-Source-Step Default initial `state_based`; nach `template_based`-Submit hat das Device-Step-Schema das flache Feld `template` (kein `entity_id`/`on_value`); nach Abschluss `_source_type_of(subentry.data)=="template_based"`.
  - **Cleanup:** `N.delete_subentry(eid,sid)`

- [ ] **CF5 — F1 Doppelter Guard-Name beim Submit abgelehnt** · `P1`
  - **Prüft:** Zwei Guards mit gleichem (case/space-insensitivem) Namen → Device-Step lehnt mit `duplicate_name` ab.
  - **Files:** `config_flow.py` → `_name_taken` `async_step_device`; de.json `config_subentries.device.error.duplicate_name`.
  - **Treiber:** `eid,sid=N.create_guard({...,"name":"DupGuard",...})`. Neuen Subentry-Flow bis Device-Step treiben, Device-Step mit `"name":" dupguard "` posten.
  - **Assert:** Antwort `step_id=="device"`, `errors=={"name":"duplicate_name"}` (kein create_entry).
  - **Cleanup:** `N.delete_subentry(eid,sid)`

- [ ] **CF6 — F6 Leere Aktion beim Submit abgelehnt** · `P1`
  - **Prüft:** `action`-Strategie ohne Aktionsinhalt → `action_required`.
  - **Files:** `config_flow.py` → `async_step_action` (`if not flat.get(CONF_ACTION): errors[CONF_ACTION]="action_required"`); de.json `...error.action_required`.
  - **Treiber:** Flow bis Action-Step (`"strategy":"action"`); Action-Step mit leerer/fehlender `action` posten.
  - **Assert:** Antwort `step_id=="action"`, `errors=={"action":"action_required"}`.
  - **Cleanup:** Flow nicht abgeschlossen → „—"

### P2 — Kosmetik / Infra

- [ ] **KOS1 — Übersetzungen symmetrisch & jeder Step beschrieben** · `P2`
  - **Prüft:** `translations/en.json` und `translations/de.json` haben dieselben Schlüssel; alle Subentry-Steps (user/reconfigure/device/strategy/switch/action/actions/poe_port/notify) tragen eine `description`.
  - **Files:** `translations/{en,de}.json` (Custom Components nutzen kein `strings.json`).
  - **Treiber:** JSON-Keys von `translations/en.json` vs `translations/de.json` vergleichen; je Step `config_subentries.device.step.<id>.description` prüfen.
  - **Assert:** Schlüsselmengen identisch (`en==de`, verifiziert: beide gleich); alle 9 Device-Steps haben nicht-leere `description` (verifiziert: user/reconfigure/device/strategy/switch/action/actions/poe_port/notify).
  - **Cleanup:** „—"

- [ ] **KOS2 — Button heißt „Reparieren", Slug `_reparieren`** · `P2`
  - **Prüft:** Die deutsche Button-Übersetzung ist „Reparieren" → entity_id `button.<slug>_reparieren` (nicht `_recover`).
  - **Files:** `translations/de.json` → `entity.button.recover.name == "Reparieren"` (verifiziert); `button.py` → `_attr_translation_key="recover"`.
  - **Treiber:** Recover-Guard `eid,sid=N.create_guard({...,"name":"KosBtn",...})` anlegen; `N.st("button.kosbtn_reparieren")`.
  - **Assert:** `N.st("button.kosbtn_reparieren")≠None`; `N.st("button.kosbtn_recover")==None`.
  - **Cleanup:** `N.delete_subentry(eid,sid)`

- [ ] **KOS3 — Status-Sensor lokalisiert alle 7 GState-Werte** · `P2`
  - **Prüft:** Die 7 Zustände ok/suspect/recovering/verify/cooldown/escalated/snoozed haben de-Übersetzungen unter `entity.sensor.status.state`.
  - **Files:** `translations/de.json` → `entity.sensor.status.state` (alle 7 Keys, verifiziert); `core/state.py` → `GState`.
  - **Treiber:** Datei-basiert.
  - **Assert:** Die 7 Keys `ok/suspect/recovering/verify/cooldown/escalated/snoozed` existieren in de.json (verifiziert vorhanden).
  - **Cleanup:** „—"

### Nach Refactors zuerst prüfen

- **LinkCoordinator-Extraktion (M1) + `core/state.py`:** Linking lebt jetzt in `core/links.py` (`LinkCoordinator`, `engine.links`), `GState` in `core/state.py` (engine re-exportiert via `from .state import GState`). Peers über `peer.links` (public, z. B. `partner.links.following` in `find_repairing_partner`), NICHT `partner._following`. Treffer-Tests: DLN1–DLN5 (Device-Naming bleibt vom Refactor unberührt, aber `link_device_id`-Pfad bestätigen), SM2/SM4 (Busy-Guard/`_cycle_task` deckt auch den Follow-up-Verify ab — `async_manual_recover`/`on_partner_repair_done` belegen denselben `_cycle_task`-Slot, core/links.py).
- **Engine `_run_recovery_cycle` (kein falscher Erfolg bei recover()-Exception):** SM4 (Max-Attempts/Verify-Timeout, terminaler ERROR ohne Traceback; recover()-Exception-Pfad ist separat und würde `LOGGER.exception` loggen).
- **F1/F6 Submit-Validierung:** CF5 (`duplicate_name`), CF6 (`action_required`) — wirklich Block, nicht nur Warnung.
- **Notify-als-Aktion (`{{ message }}`-Variablen via Script):** NOT1/NOT3/NOT4 (Variablen + EINE Meldung), NOT2 (defekter Service gefangen).
- **Reconfigure-Defaults/Source-Wechsel:** CF3 (alle Felder vorbefüllt), CF4 (state↔template).
- **Auto-Reload + Store-Flush:** CF2 (anderer Guard unberührt, recover_count überlebt Reload).

---

## Operator-Services & Recovery-Event

> Primär **automatisiert** abgedeckt: `tests/suite/test_services.py` (8) +
> `tests/suite/test_event.py` (4) + `tests/suite/test_health_primitives.py` (7, die
> Response-Services). Hier die Live-Smoke-Treiber (de-Slugs beachten —
> Status-Sensor `sensor.<slug>_status`, Event `event.<slug>_wiederbelebung`).

- [ ] **SVC-1 — `reset` löscht ESCALATED** · `P1`
  - **Prüft:** `necromancer.reset` auf einen eskalierten Guard → `OK`, dann Neu-Bewertung (noch krank → `suspect`, gesund → bleibt `ok`); keine sinnlose Reparatur.
  - **Treiber:** Guard eskalieren lassen; `N.call("necromancer","reset",entity_id="sensor.<slug>_status")`.
  - **Assert:** `N.guard("<slug>")[0]` in `("ok","suspect")`; pytest `test_reset_clears_escalation`.
- [ ] **SVC-2 — `snooze`/`unsnooze`** · `P1`
  - **Prüft:** `snooze {duration}` → `snoozed`, ignoriert Health (Break → **kein** SUSPECT), `snooze_until`-Attribut; Auto-Resume nach Ablauf; `unsnooze` sofort; **mid-recovery → `ServiceValidationError`**; übersteht Neustart (Restzeit).
  - **Treiber:** `N.call("necromancer","snooze",entity_id="sensor.<slug>_status",duration={"hours":1})`; Health brechen; Status bleibt `snoozed`.
  - **Assert:** Status `snoozed`/zurück; pytest `test_snooze_*` / `test_unsnooze_*` / `test_snooze_during_recovery_raises`.
- [ ] **SVC-3 — `snooze_all`/`unsnooze_all`** · `P1`
  - **Prüft:** Domain-Service (kein Target) snoozt/entsnoozt **alle** Guards; busy-Guards übersprungen (WARNING `snooze_all: skipped … busy`).
  - **Treiber:** `N.call("necromancer","snooze_all",duration={"minutes":30})`; alle Status `snoozed`. `unsnooze_all` → alle zurück.
  - **Assert:** alle `snoozed`/zurück; pytest `test_snooze_all_*`.
- [ ] **SVC-4 — Response-Services `check_health`/`wait_for_health`** · `P1`
  - **Prüft:** `necromancer.check_health` liefert (response-only) den aktuellen Health State des Guards (`{"health": ...}` via `engine.current_health()`); `necromancer.wait_for_health` wartet bis Health=OK oder Timeout und liefert `{"health", "timed_out", "waited_s"}` (eigener Waiter, nicht das VERIFY-Event). `wait_for_health` re-nutzt den Health Check des Guards; `check_first` (default an) prüft sofort, ohne Default-Timeout greift `boot_window`.
  - **Files:** `__init__.py` → `_check_health` (`SupportsResponse.ONLY`), `_wait_for_health` (`SupportsResponse.ONLY`); `core/engine.py` → `current_health` `async_service_wait_health`; `services.yaml` `check_health`/`wait_for_health`.
  - **Treiber:** `N.call("necromancer","check_health",entity_id="sensor.<slug>_status",return_response=True)`; Health brechen + heilbares `*_check`-Guard → `N.call("necromancer","wait_for_health",entity_id="sensor.<slug>_status",timeout=30,return_response=True)`.
  - **Assert:** `check_health`-Response `health` in `("ok","unhealthy","unknown")`; `wait_for_health`-Response hat `health`/`timed_out`/`waited_s`; pytest `test_check_health_returns_verdict`, `test_wait_for_health_already_ok_returns_at_once`, `test_wait_for_health_heals_during_wait`, `test_wait_for_health_timeout_defaults_to_boot_window`.
- [ ] **EVT-1 — Recovery-Event** · `P1`
  - **Prüft:** `event.<slug>_…` feuert `recovered` (Erfolg), `escalated` (Aufgabe nach max_attempts), `blocked` (Pre-Flight, Ziel fehlt). Nur Recover-Guards; notify-only hat keins.
  - **Treiber:** Erfolg / Eskalation / Block provozieren; `N.st("event.<slug>_…")["attributes"]["event_type"]`.
  - **Assert:** `event_type` passt; pytest `test_recovered_event` / `test_escalated_event` / `test_blocked_event`.

---

## Lücken / Ergänzungen (Completeness-Kritiker)

### Lücken

- [ ] **GAP-B1a — Stale-Cache via Re-Cabling-Simulation (Live)** · `P1`
  - **Prüft:** Wurde ein Gerät A umgesteckt und sitzt jetzt B auf dem alten Port, darf der Guard für A NICHT den Port zyklen (sonst Reboot des unschuldigen B); der stale Cache-Eintrag wird verworfen.
  - **Files:** `core/poe.py` → `resolve_with_reason` (occupant-Check: `occupant is None` ⇒ last-known erlaubt; sonst `pop(target)` + WARNING `"now serves … — dropping stale cache"`).
  - **Treiber:** Port mit `id_entity=sensor.test_device_info`, `id_attribute="mac"` via `N.add_port({...})` anlegen; `N.setstate("sensor.test_device_info","x",mac="aa:aa")` (A lernen) → `N.wait(1)`; dann `N.setstate("sensor.test_device_info","x",mac="bb:bb")` (B sitzt jetzt drauf) → `N.wait(1)`; danach `N.call("necromancer","repair_poe_port",id="aa:aa")`.
  - **Assert:** `N.log()` enthält `"now serves 'bb:bb' — dropping stale cache"` UND `"cannot repair 'aa:aa'"`; KEIN `"cutting power"` für diesen Port nach dem repair-Aufruf.
  - **Cleanup:** `N.remove_port("<label>")`

- [ ] **GAP-B1b — last-known greift nur bei leerem Port** · `P1`
  - **Prüft:** Meldet der gecachte Port aktuell *nichts* (Placeholder), bleibt der last-known-Fallback gültig und liefert den Port zurück.
  - **Files:** `core/poe.py` → `resolve_with_reason` (`occupant is None` ⇒ WARNING `"not in any port's neighbour data — last-known port"`, return port).
  - **Treiber:** `N.add_port({... id_entity:"sensor.test_device_info", id_attribute:"mac"})`; `N.setstate(...,mac="aa:aa")`→`N.wait(1)`; dann `N.setstate("sensor.test_device_info","x")` (mac-Attr weg ⇒ Port leer)→`N.wait(1)`; `N.call("necromancer","repair_poe_port",id="aa:aa")`.
  - **Assert:** `N.log()` enthält `"last-known port"` UND einen Cycle-Marker `"cutting power"`; der Cache-Eintrag `aa:aa` bleibt erhalten.
  - **Cleanup:** `N.remove_port("<label>")`

- [ ] **GAP-B1c — Unit-Test test_resolve_last_known_skips_occupied_port existiert** · `P2`
  - **Prüft:** Der neue PoE-Unit-Test für B1 ist vorhanden und im Lauf grün (Regressionsanker für die Stale-Cache-Logik).
  - **Files:** `tests/test_poe.py` → `test_resolve_last_known_skips_occupied_port` (asserts `p is None`, `"no port matches"`, `f.cache.get("aa:aa") is None`).
  - **Treiber:** `uv run python` aus `<ha-core>` mit `PYTHONPATH=<ha-core>:<ha-core>/config` → `python tests/test_poe.py`.
  - **Assert:** Ausgabe enthält `ok    test_resolve_last_known_skips_occupied_port` und Schlusszeile `16 passed, 0 failed`.
  - **Cleanup:** —

- [ ] **GAP-B2a — Reload mitten im Recovery-Zyklus eskaliert Follower NICHT (Datei-Marker)** · `P0`
  - **Prüft:** Wird der Leader-Engine mid-cycle gestoppt (Reload/Unload), darf sein `finally` KEIN failed-`notify_done` an die Gruppe feuern (sonst Eskalation der Follower aus halbem Zyklus).
  - **Files:** `core/engine.py` → `_run_recovery_cycle` `finally` (`if not self._stopping: self.links.notify_done(...)`); `async_stop` setzt `_stopping=True` + `links.reset()` VOR dem Cancel; `core/links.py` → `validate_after_repair` `finally` (cycle-slot wird auch bei Cancel geleert).
  - **Treiber:** rein datei-basiert bestätigen (Live zu kurz/zu flüchtig).
  - **Assert:** `_stopping = True` steht in `async_stop` VOR `if self._cycle_task … cancel()`; `notify_done` ist durch `if not self._stopping` geschützt.
  - **Cleanup:** —

- [ ] **GAP-B2b — async_stop resettet Link-Zustand** · `P1`
  - **Prüft:** Beim Teardown wird Follower-State (`following`/`leader`) zurückgesetzt, damit ein neu geladener Guard nicht als „hängender Follower" startet.
  - **Files:** `core/engine.py` → `async_stop` `self.links.reset()`; `core/links.py` → `LinkCoordinator.reset` (`following=False; leader=None`).
  - **Treiber:** Engine-Suite ist der Anker — `python tests/test_engine.py`; relevante Fälle `test_async_stop_cancels_validate_no_escalation`, `test_leader_stop_does_not_escalate_follower`.
  - **Assert:** Ausgabe enthält `ok    test_async_stop_cancels_validate_no_escalation` und `ok    test_leader_stop_does_not_escalate_follower`; `34 passed, 0 failed`.
  - **Cleanup:** —

- [ ] **GAP-M1a — GState aus core/state.py ausgelagert, engine re-exportiert weiterhin** · `P0`
  - **Prüft:** Nach dem M1-Refactor ist `GState` in `core/state.py` und sowohl `from .engine import GState` als auch `from .state import GState` funktionieren (sensor.py + Tests hängen an `engine.GState`).
  - **Files:** `core/state.py` → `class GState(StrEnum)` (7 Werte); `core/engine.py` `from .state import GState` (macht `engine.GState` verfügbar); `sensor.py` `from .core.engine import DeviceEngine, GState`.
  - **Treiber:** `uv run python -c "import sys; sys.path.insert(0,'repo'); from custom_components.necromancer.core.engine import GState as A; from custom_components.necromancer.core.state import GState as B; print(A is B, [s.value for s in A])"` (aus `<ha-core>`, PYTHONPATH gesetzt).
  - **Assert:** Ausgabe `True ['ok', 'suspect', 'recovering', 'verify', 'cooldown', 'escalated', 'snoozed']`.
  - **Cleanup:** —

- [ ] **GAP-M1b — Status-Sensor lädt + ENUM-Optionen korrekt (Live)** · `P0`
  - **Prüft:** Nach dem GState-Move lädt die Sensor-Plattform weiter; der Status-Sensor existiert und seine ENUM-`options` decken alle 7 States ab.
  - **Treiber:** Guard anlegen `eid,sub = N.create_guard({...minimal recover...})` → `N.wait(2)`; dann `N.st("sensor.<slug>_status")`.
  - **Assert:** `st["state"] == "ok"`; `st["attributes"]["options"] == ["ok","suspect","recovering","verify","cooldown","escalated","snoozed"]`; `st["attributes"]["device_class"] == "enum"`.
  - **Cleanup:** `N.delete_subentry(eid, sub)`

- [ ] **GAP-M1c — Peer-Zugriff nur über peer.links (kein partner._following)** · `P1`
  - **Prüft:** Der LinkCoordinator erreicht Partner ausschließlich über die öffentliche `peer.links`-Fassade, nicht über alte private Attribute (`partner._following`/`partner._on_partner_repair_*`).
  - **Files:** `core/links.py` → `find_repairing_partner` (`partner.links.following`), `notify_start`/`notify_done` (`partner.links.on_partner_repair_…`). Bestätigen: in `core/links.py` taucht KEIN `partner._following` / `partner._on_partner_repair` auf.
  - **Treiber:** `grep -nE "partner\._(following|on_partner)" custom_components/necromancer/core/links.py` → muss leer sein.
  - **Assert:** grep liefert 0 Treffer; `partner.links.following` und `partner.links.on_partner_repair_start` sind vorhanden.
  - **Cleanup:** —

- [ ] **GAP-PE1 — PoE-Cache in den Store persistiert (_poe_cache)** · `P1`
  - **Prüft:** Der gelernte id→Port-Cache wird unter `_poe_cache` serialisiert und beim Setup wieder in die Fabric geseedet (überlebt Reload/Neustart).
  - **Files:** `__init__.py` → `_serialize` (`data["_poe_cache"] = fabric.cache`); `fabric.set_ports(ports, cache=stored.get("_poe_cache"))`; `core/poe.py` → `set_ports` `cache`-Param.
  - **Treiber:** Port mit dynamischer id anlegen, Gerät lernen lassen (`N.setstate(..., mac="aa:bb")`→`N.wait(1)`); dann Storage-Datei lesen: `N.g` gibt es nicht für Files → bash `grep -l _poe_cache <ha-core>/config/.storage/necromancer.*` nach `N.wait(6)` (SAVE_DELAY=5).
  - **Assert:** Storage-JSON enthält Key `"_poe_cache"` mit `"aa:bb"` → Port-Label.
  - **Cleanup:** `N.remove_port("<label>")`

- [ ] **GAP-PE2 — Placeholder-Ids werden nie gelernt (kein WARNING-Storm)** · `P2`
  - **Prüft:** Ports ohne angeschlossenes Gerät melden Platzhalter (`-`/`unknown`/leer); die Fabric lernt daraus nichts und ein Platzhalter-Identifier resolved auf nichts.
  - **Files:** `core/poe.py` → `_PLACEHOLDER_IDS` `_norm` `_relearn`; Unit-Test `tests/test_poe.py::test_placeholder_ids_are_never_learned`.
  - **Treiber:** `python tests/test_poe.py` (Unit-Anker) ODER live: `N.add_port({...id_entity:"sensor.test_device_info"})` ohne mac-Attr → `N.setstate("sensor.test_device_info","-")` → `N.wait(1)` → `N.call("necromancer","repair_poe_port",id="-")`.
  - **Assert:** Unit-Lauf zeigt `ok    test_placeholder_ids_are_never_learned`; live: `N.log()` enthält `"no port matches '-'"`, KEIN „learned"/„moved" WARNING.
  - **Cleanup:** `N.remove_port("<label>")`

- [ ] **GAP-CC1 — HA-Restart mid-cycle: transienter State wird aus Live-Health neu abgeleitet** · `P1`
  - **Prüft:** Nach Neustart werden transiente States (RECOVERING/VERIFY/SUSPECT) NICHT aus dem Store restauriert; nur ESCALATED bleibt terminal, Stats+`auto` bleiben.
  - **Files:** `core/engine.py` → `_apply_persisted` (nur `state == "escalated"` wird gesetzt; transient ⇒ `async_start`/`_evaluate` leitet neu ab); Unit-Anker `test_engine.py::test_persistence_escalated_stays` / `_autoclears`.
  - **Treiber:** `python tests/test_engine.py`.
  - **Assert:** `ok    test_persistence_escalated_stays`, `ok    test_persistence_escalated_autoclears`, `ok    test_snapshot_roundtrip`.
  - **Cleanup:** —

- [ ] **GAP-CC2 — Store-Flush vor Teardown (kein staler Store nach Reload)** · `P1`
  - **Prüft:** `async_unload_entry` schreibt den serialisierten State synchron weg, BEVOR Engines gestoppt werden, damit ein Reload (Rename/Reconfigure) keinen veralteten Store liest.
  - **Files:** `__init__.py` → `async_unload_entry` (`store.async_save(serialize())` vor `engine.async_stop()`); `_save` nutzt `async_delay_save` (SAVE_DELAY).
  - **Treiber:** datei-basiert bestätigen (Reihenfolge der Aufrufe in `async_unload_entry`).
  - **Assert:** Im `async_unload_entry` steht `await store.async_save(serialize())` vor der Engine-Stop-Schleife `for engine in entry.runtime_data.values(): await engine.async_stop()`.
  - **Cleanup:** —

- [ ] **GAP-CC3 — Storage-Migration-Gerüst (STORAGE_VERSION=1, keine Migration nötig)** · `P2`
  - **Prüft:** Es gibt (noch) keinen `async_migrate`-Pfad; STORAGE_VERSION ist 1 und `async_load() or {}` toleriert ein leeres/fehlendes Store-File. Lücke dokumentieren, falls später Version steigt.
  - **Files:** `const.py` → `STORAGE_VERSION = 1`; `__init__.py` (`Store(hass, STORAGE_VERSION, …)`, `async_load() or {}`). Bestätigen: KEIN `async_migrate_func`/migrator registriert.
  - **Treiber:** `grep -rn "migrate\|STORAGE_VERSION" custom_components/necromancer/` .
  - **Assert:** STORAGE_VERSION == 1; kein `migrate`-Treffer ⇒ bei einem künftigen Bump MUSS ein Migrator + Test ergänzt werden (Lücke notieren).
  - **Cleanup:** —

- [ ] **GAP-RD1 — repair_poe_port-Service nur einmal registriert (Reload-sicher)** · `P2`
  - **Prüft:** Der Service `necromancer.repair_poe_port` wird über `has_service` geschützt nur einmal registriert; ein Reload re-registriert nicht und die Fabric bleibt Domain-Singleton.
  - **Files:** `__init__.py` → (`if not hass.services.has_service(...)`); Fabric-Singleton (`domain_data.get("fabric") or PoeFabric(hass)`).
  - **Treiber:** Live: `N.g("/api/services")` und nach Service `necromancer`/`repair_poe_port` suchen; Reload erzwingen (Port-Options-Change via `N.add_port`/`N.remove_port`) → erneut prüfen.
  - **Assert:** Service `repair_poe_port` ist genau einmal vorhanden, vor und nach Reload identisch (keine Exception im `N.log()` über doppelte Registrierung).
  - **Cleanup:** `N.remove_port("<label>")`

- [ ] **GAP-LK1 — notify-only-Guard fällt aus allen Link-Gruppen (Closure schließt ihn aus)** · `P1`
  - **Prüft:** Ein zu notify-only rekonfigurierter Guard ist kein Link-Ziel mehr — `_is_recover` filtert ihn aus `device_ids`+`declared_links`, sodass `link_components` ihn nicht in eine Gruppe zieht.
  - **Files:** `__init__.py` → `_is_recover` + `device_ids`/`declared_links`; `core/links.py` → `link_components` (stale ids via `valid` gedroppt).
  - **Treiber:** Zwei recover-Guards verlinken (`linked_guards` im zweiten Spec), bestätigen dass beide verlinkt sind (`N.guard(slug)` attrs/Log `linked=`), dann einen auf notify-only umkonfigurieren (oder als notify anlegen) → Reload.
  - **Assert:** Im Setup-Log `Guard … linked=` zeigt für den notify-only-Guard `linked=none`; der Partner zeigt den notify-only-Guard NICHT mehr in seiner Gruppe.
  - **Cleanup:** `N.delete_subentry(...)` für beide.

- [ ] **GAP-RC1 — recover→notify-only entfernt Switch+Button-Waisen** · `P1`
  - **Prüft:** Wird ein Guard von recover auf notify-only umgestellt, werden seine Steuer-Entities (`switch._auto_reparatur`, `button._reparieren`) aus der Registry entfernt (keine Orphans).
  - **Files:** `__init__.py` → `_reconcile_entities` (entfernt `switch/auto_restart` + `button/recover` für `not engine.allows_recovery`).
  - **Treiber:** recover-Guard anlegen → `N.st("switch.<slug>_auto_reparatur")` ist gesetzt; via Reconfigure auf notify-only umstellen → Reload → erneut `N.st(...)` für Switch und Button.
  - **Assert:** `N.st("switch.<slug>_auto_reparatur") is None` UND `N.st("button.<slug>_reparieren") is None` nach der Umstellung; `N.log()` enthält `"Removing"` … `"(notify-only guard"`.
  - **Cleanup:** `N.delete_subentry(...)`

- [ ] **GAP-CFG1 — Config-Error-Logging beim Start (fehlende Health-Entity)** · `P1`
  - **Prüft:** `_check_config` läuft erst nach „HA started" und loggt fehlende/disabled Health-Entities als ERROR (Boot-Race vermieden).
  - **Files:** `core/engine.py` → `_check_config` (`async_at_started` Hook in `__init__.py`; ERROR „health entity … does not exist" / „is disabled — guard is blind"); Integration-Anker `test_integration.py::test_health_disable_logs_blind`.
  - **Treiber:** `python tests/test_integration.py` (Anker) ODER live: Guard mit nicht existierender Health-Entity anlegen → `N.wait(2)` → `N.log()`.
  - **Assert:** Integration: `ok  health:disable_logs_blind`; live: `N.log()` enthält `"does not exist"` (kein Traceback).
  - **Cleanup:** `N.delete_subentry(...)`

- [ ] **GAP-CC4 — _await_status: bereits-im-Zielzustand-Port (kein Timeout-Warten)** · `P2`
  - **Prüft:** Meldet die Status-Entity beim Power-On bereits den Online-Wert, kehrt `_await_status` sofort `True` zurück (kein unnötiges Timeout/Race auf das State-Event).
  - **Files:** `core/poe.py` → `_await_status` (`if current() in targets: return True` vor dem Event-Abonnement).
  - **Treiber:** Anker `tests/test_poe.py::test_repair_cycles_and_fires_status` deckt den Pfad; Lauf bestätigen.
  - **Assert:** `ok    test_repair_cycles_and_fires_status` im PoE-Lauf; `f.status("PX") == "good"`.
  - **Cleanup:** —

- [ ] **GAP-CC5 — Ambiguer Resolve verweigert (>1 Live-Match)** · `P2`
  - **Prüft:** Melden zwei Ports dieselbe id, rät die Fabric NICHT, sondern verweigert mit `"matches N ports"` (kein Cycle des falschen Ports).
  - **Files:** `core/poe.py` → `resolve_with_reason` (`len(live) > 1` ⇒ `None, "matches N ports"`); Unit `test_poe.py::test_resolve_ambiguous`.
  - **Treiber:** `python tests/test_poe.py`.
  - **Assert:** `ok    test_resolve_ambiguous`.
  - **Cleanup:** —

- [ ] **GAP-CC6 — Coalescing ersetzt per-Port-Lock (richtige Test-Namen)** · `P1`
  - **Prüft:** Der per-Port asyncio.Lock ist entfernt; gleichzeitige Repair-Aufrufe coalescen auf genau einen Cycle (`_inflight` + `asyncio.shield`). Veraltete Testnamen existieren NICHT mehr.
  - **Files:** `core/poe.py` → `repair` (`self._inflight`, `asyncio.shield(task)`), KEIN `asyncio.Lock`; Tests `test_concurrent_callers_coalesce` + `test_driver_and_service_coalesce` (NICHT `*_per_port_lock_*`).
  - **Treiber:** `grep -n "asyncio.Lock\|per_port_lock\|share_lock" custom_components/necromancer/core/poe.py tests/test_poe.py` → leer; dann `python tests/test_poe.py`.
  - **Assert:** grep 0 Treffer; Lauf zeigt `ok    test_concurrent_callers_coalesce` + `ok    test_driver_and_service_coalesce`, `cycles == 1`/`max_conc == 1`.
  - **Cleanup:** —

- [ ] **GAP-SUITE — Aktuelle Suite-Zählungen stimmen (Doc-Drift gegen Code)** · `P2`
  - **Prüft:** Die vier In-Process-Suiten melden die aktuellen Counts (units=29, poe=16, engine=34, integration=12-Checks); zusätzlich die pytest-Suite `tests/components/necromancer/` (72) via `pytest tests/components/necromancer/`.
  - **Treiber:** je `python tests/test_units.py`, `…/test_poe.py`, `…/test_engine.py`, `…/test_integration.py` (aus `<ha-core>`, PYTHONPATH gesetzt); `<ha-venv>/bin/python -m pytest tests/components/necromancer/`.
  - **Assert:** Schlusszeilen `29 passed`, `16 passed`, `34 passed`, `12/12 checks passed`, `72 passed` (jeweils 0 failed).
  - **Cleanup:** —
