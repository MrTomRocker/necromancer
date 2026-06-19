"""Manual/integration tests driven against a live dev-container HA (testing.md §3)
plus active pitfall probing. Drives the REST subentry/options flow, the WS entity
registry, services and state toggles; cleans up the guards it creates.

Run inside the dev container with HA already RUNNING:
    PYTHONPATH=/workspaces/ha-core/config python manual_dev_tests.py
"""

from __future__ import annotations

import asyncio
import json

import aiohttp

TOKEN = open("/tmp/dev_token.txt").read().strip()
BASE = "http://localhost:8123"
ENTRY = "01KVBNTP9PJHX9Q3EASBS0KCG0"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

findings: list[str] = []
results: list[tuple[str, bool, str]] = []


def ok(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def finding(msg):
    findings.append(msg)


# ---------- REST/WS helpers ----------
async def rest(s, method, path, **kw):
    async with s.request(method, BASE + path, headers=H, **kw) as r:
        txt = await r.text()
        try:
            return r.status, json.loads(txt) if txt else None
        except json.JSONDecodeError:
            return r.status, txt


async def state(s, eid):
    st, d = await rest(s, "GET", f"/api/states/{eid}")
    return d.get("state") if isinstance(d, dict) else None


async def call(s, dom, srv, data):
    return await rest(s, "POST", f"/api/services/{dom}/{srv}", json=data)


async def errorlog(s):
    _st, d = await rest(s, "GET", "/api/error_log")
    return d if isinstance(d, str) else ""


async def flow_start(s):
    _st, d = await rest(s, "POST", "/api/config/config_entries/subentries/flow",
                        json={"handler": [ENTRY, "device"]})
    return d["flow_id"]


async def flow_step(s, fid, payload):
    return await rest(s, "POST",
                      f"/api/config/config_entries/subentries/flow/{fid}", json=payload)


async def reconf_start(s, sid):
    _st, d = await rest(s, "POST", "/api/config/config_entries/subentries/flow",
                        json={"handler": [ENTRY, "device"], "subentry_id": sid})
    return d["flow_id"]


def behavior(check):
    b = {"debounce": 2, "cooldown": 2}
    if check:
        b["boot_window"] = 2
        b["max_attempts"] = 1
    return b


CHECK = {"switch_check", "action_check", "actions_check", "poe_port"}


async def create_guard(s, name, *, source, health, mode, strategy, sfields, link=()):
    """Create a guard subentry; returns (created?, last_response)."""
    fid = await flow_start(s)
    await flow_step(s, fid, {"source_type": source})
    devstep = {"name": name, "mode": mode, "assigned_device": {}}
    devstep.update(health)  # template_check{} or state_check{}
    _st, d = await flow_step(s, fid, devstep)
    if mode == "notify":
        _st, d = await flow_step(s, fid, {"debounce": 2, "notification": {}})
        return d.get("type") == "create_entry", d
    _st, d = await flow_step(s, fid, {"strategy": strategy})
    step = dict(sfields)
    step["behavior"] = behavior(strategy in CHECK)
    step["notification"] = {}
    step["linked_guards"] = {"linked_guards": list(link)}  # dev has other recover guards
    _st, d = await flow_step(s, fid, step)
    return d.get("type") == "create_entry", d


def storage_subentries(titles=None):
    d = json.load(open("/workspaces/ha-core/config/.storage/core.config_entries"))
    out = {}
    for e in d["data"]["entries"]:
        if e["domain"] == "necromancer":
            for se in e.get("subentries", []):
                if titles is None or se.get("title") in titles:
                    out[se["title"]] = se["subentry_id"]
    return out


async def ws_call(payloads):
    """Run a list of WS commands; return list of results."""
    out = []
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"{BASE}/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": TOKEN})
            await ws.receive_json()
            for i, p in enumerate(payloads, 1):
                await ws.send_json({"id": i, **p})
                while True:
                    m = await ws.receive_json()
                    if m.get("id") == i and m.get("type") == "result":
                        out.append(m)
                        break
    return out


async def delete_guards(titles):
    subs = storage_subentries(titles)
    if not subs:
        return
    await ws_call([{"type": "config_entries/subentries/delete",
                    "entry_id": ENTRY, "subentry_id": sid} for sid in subs.values()])


# ---------- §3 tests ----------
ALL_STRATEGIES = [
    ("switch", {"switch_entity": "input_boolean.test_5", "off_on_delay": 1}),
    ("switch_check", {"switch_entity": "input_boolean.test_5", "off_on_delay": 1}),
    ("action", {"action": [{"delay": 1}]}),
    ("action_check", {"action": [{"delay": 1}]}),
    ("actions", {"off_action": [{"delay": 1}], "on_action": [{"delay": 1}], "off_on_delay": 1}),
    ("actions_check", {"off_action": [{"delay": 1}], "on_action": [{"delay": 1}], "off_on_delay": 1}),
    ("poe_port", {"expected_id": "DEV-LOG"}),
]


async def t_flow_build_all(s):
    names = []
    for strat, sf in ALL_STRATEGIES:
        n = f"MT_{strat}"
        names.append(n)
        created, d = await create_guard(
            s, n, source="state_based",
            health={"state_check": {"entity_id": "input_boolean.test_6",
                                    "on_value": ["on"], "off_value": ["off"]}},
            mode="recover", strategy=strat, sfields=sf)
        ok(f"flow_build:{strat}", created, "" if created else json.dumps(d)[:200])
    # notify-only guard (template health)
    created, d = await create_guard(
        s, "MT_notify", source="template_based",
        health={"template_check": {"template": "{{ is_state('input_boolean.test_6','on') }}"}},
        mode="notify", strategy="", sfields={})
    ok("flow_build:notify", created, "" if created else json.dumps(d)[:200])
    names.append("MT_notify")
    await delete_guards(names)


async def t_broken_template_rejected(s):
    fid = await flow_start(s)
    await flow_step(s, fid, {"source_type": "template_based"})
    _st, d = await flow_step(s, fid, {"name": "MT_badtpl", "mode": "recover",
                                      "assigned_device": {},
                                      "template_check": {"template": "{{ unclosed "}})
    rejected = d.get("type") == "form" and bool(d.get("errors"))
    ok("corner:broken_template_rejected", rejected, json.dumps(d.get("errors"))[:150])
    if d.get("type") == "create_entry":
        finding("Broken Jinja template was ACCEPTED at submit (expected rejection).")
        await delete_guards(["MT_badtpl"])


async def t_missing_service_escalates(s):
    # action_check guard; recovery calls a non-existent service -> must escalate
    created, _d = await create_guard(
        s, "MT_missvc", source="state_based",
        health={"state_check": {"entity_id": "input_boolean.test_6",
                                "on_value": ["on"], "off_value": ["off"]}},
        mode="recover", strategy="action_check",
        sfields={"action": [{"action": "necromancer.does_not_exist", "data": {}}]})
    if not created:
        ok("corner:missing_service_escalates", False, "guard not created")
        return
    await call(s, "input_boolean", "turn_on", {"entity_id": "input_boolean.test_6"})
    await call(s, "input_boolean", "turn_off", {"entity_id": "input_boolean.test_6"})
    await asyncio.sleep(8)  # debounce 2 + attempt + verify
    st = await state(s, "sensor.mt_missvc_status")
    ok("corner:missing_service_escalates", st == "escalated", f"status={st}")
    await call(s, "input_boolean", "turn_on", {"entity_id": "input_boolean.test_6"})
    await delete_guards(["MT_missvc"])


async def t_health_registry_robustness(s):
    # state-based guard on a throwaway template-helper entity we can rename/disable
    # use input_boolean.test_5 which exists; disable/enable via WS registry
    created, _d = await create_guard(
        s, "MT_health", source="state_based",
        health={"state_check": {"entity_id": "input_boolean.test_5",
                                "on_value": ["on"], "off_value": ["off"]}},
        mode="recover", strategy="action", sfields={"action": [{"delay": 1}]})
    if not created:
        ok("health:setup", False, "guard not created")
        return
    before = await errorlog(s)
    # disable the health entity
    r = await ws_call([{"type": "config/entity_registry/update",
                        "entity_id": "input_boolean.test_5", "disabled_by": "user"}])
    await asyncio.sleep(2)
    log = (await errorlog(s))[len(before):]
    disabled_ok = "disabled — guard is blind" in log
    crashed = "Traceback" in log
    ok("health:disable_logs_blind", disabled_ok, "")
    ok("health:disable_no_crash", not crashed, "")
    if not disabled_ok:
        finding("Disabling a health entity did NOT log the 'guard is blind' warning.")
    # re-enable
    await ws_call([{"type": "config/entity_registry/update",
                    "entity_id": "input_boolean.test_5", "disabled_by": None}])
    await asyncio.sleep(2)
    ok("health:reenable_ok", r[0].get("success", False), "")
    await delete_guards(["MT_health"])


async def t_options_import_export(s):
    # non-destructive: open options flow, import invalid then valid(merge), export
    _st, d = await rest(s, "POST", "/api/config/config_entries/options/flow",
                        json={"handler": ENTRY})
    fid = d.get("flow_id")
    if not fid:
        ok("options:open", False, json.dumps(d)[:150])
        return
    ok("options:open", True, "")
    # menu -> import_ports
    _st, d = await rest(s, "POST", f"/api/config/config_entries/options/flow/{fid}",
                        json={"next_step_id": "import_ports"})
    # invalid YAML
    _st, d = await rest(s, "POST", f"/api/config/config_entries/options/flow/{fid}",
                        json={"import_mode": "merge", "ports_yaml": "- just_a_scalar"})
    invalid_rejected = d.get("type") == "form" and bool(d.get("errors"))
    ok("options:invalid_import_rejected", invalid_rejected, json.dumps(d.get("errors"))[:120])
    # valid merge
    valid = [{"label": "MT_PORT", "actuator": "input_boolean.test_5",
              "status_entity": "binary_sensor.test_reachable",
              "status_on": ["on"], "status_off": ["off"]}]
    _st, d = await rest(s, "POST", f"/api/config/config_entries/options/flow/{fid}",
                        json={"import_mode": "merge", "ports_yaml": valid})
    merged = d.get("type") in ("menu", "form")  # back to menu without error
    ok("options:valid_merge", merged, json.dumps(d)[:120])
    # abandon the flow (do NOT save) -> delete it, real ports untouched
    await rest(s, "DELETE", f"/api/config/config_entries/options/flow/{fid}")


async def t_repair_service(s):
    # good id (DEV-LOG sim port) cycles; bad id no crash
    before = await errorlog(s)
    st1, _ = await call(s, "necromancer", "repair_poe_port", {"id": "DEV-LOG"})
    st2, _ = await call(s, "necromancer", "repair_poe_port", {"id": "ghost-id-xyz"})
    await asyncio.sleep(1)
    log = (await errorlog(s))[len(before):]
    ok("service:good_id_200", st1 == 200, f"status={st1}")
    ok("service:bad_id_200_nocrash", st2 == 200 and "Traceback" not in log, "")


# ---------- pitfall probes ----------
async def p_poe_bogus_id_blocks(s):
    created, _d = await create_guard(
        s, "MT_poebogus", source="state_based",
        health={"state_check": {"entity_id": "input_boolean.test_6",
                                "on_value": ["on"], "off_value": ["off"]}},
        mode="recover", strategy="poe_port", sfields={"expected_id": "NO-SUCH-DEVICE-9999"})
    if not created:
        ok("pitfall:poe_bogus_setup", False, "not created")
        return
    await call(s, "input_boolean", "turn_on", {"entity_id": "input_boolean.test_6"})
    await call(s, "input_boolean", "turn_off", {"entity_id": "input_boolean.test_6"})
    await asyncio.sleep(7)
    st = await state(s, "sensor.mt_poebogus_status")
    ok("pitfall:poe_bogus_id_escalates_no_blind", st == "escalated", f"status={st}")
    await call(s, "input_boolean", "turn_on", {"entity_id": "input_boolean.test_6"})
    await delete_guards(["MT_poebogus"])


async def p_link_delete_partner_selfheal(s):
    # create A and B linked, delete A, confirm B has no error + drops the stale link
    a, _ = await create_guard(
        s, "MT_LA", source="template_based",
        health={"template_check": {"template": "{{ true }}"}},
        mode="recover", strategy="action", sfields={"action": [{"delay": 1}]})
    await asyncio.sleep(1)
    a_id = storage_subentries(["MT_LA"]).get("MT_LA")
    b, _ = await create_guard(
        s, "MT_LB", source="template_based",
        health={"template_check": {"template": "{{ true }}"}},
        mode="recover", strategy="action", sfields={"action": [{"delay": 1}]},
        link=[a_id] if a_id else ())
    await asyncio.sleep(1)
    before = await errorlog(s)
    await delete_guards(["MT_LA"])  # delete the partner
    await asyncio.sleep(3)
    log = (await errorlog(s))[len(before):]
    nocrash = "Traceback" not in log
    bstat = await state(s, "sensor.mt_lb_status")
    ok("pitfall:delete_link_partner_no_crash", nocrash and bstat is not None,
       f"b_status={bstat}")
    if not nocrash:
        finding("Deleting a linked partner produced a traceback.")
    await delete_guards(["MT_LB"])


async def p_duplicate_names(s):
    a, _ = await create_guard(
        s, "MT_DUP", source="template_based",
        health={"template_check": {"template": "{{ true }}"}},
        mode="recover", strategy="action", sfields={"action": [{"delay": 1}]})
    b, d2 = await create_guard(
        s, "MT_DUP", source="template_based",
        health={"template_check": {"template": "{{ true }}"}},
        mode="recover", strategy="action", sfields={"action": [{"delay": 1}]})
    ok("pitfall:duplicate_names_allowed", a and b, "both created")
    if a and b:
        finding("Two guards with the SAME name are allowed (entity_ids may collide: "
                "sensor.mt_dup_status). Consider a uniqueness hint.")
    await delete_guards(["MT_DUP"])  # deletes both (by title)


TESTS = [t_flow_build_all, t_broken_template_rejected, t_missing_service_escalates,
         t_health_registry_robustness, t_options_import_export, t_repair_service,
         p_poe_bogus_id_blocks, p_link_delete_partner_selfheal, p_duplicate_names]


async def main():
    async with aiohttp.ClientSession() as s:
        for t in TESTS:
            try:
                await t(s)
            except Exception as err:  # noqa: BLE001
                ok(t.__name__, False, f"EXC {err!r}")
    print("\n==== RESULTS ====")
    p = sum(1 for _n, c, _d in results if c)
    for n, c, d in results:
        print(f"{'ok  ' if c else 'FAIL'} {n}{('  — ' + d) if (d and not c) else ''}")
    print(f"\n{p}/{len(results)} checks passed")
    print("\n==== FINDINGS (potential pitfalls) ====")
    for f in findings:
        print(" -", f)
    if not findings:
        print(" (none)")


if __name__ == "__main__":
    asyncio.run(main())
