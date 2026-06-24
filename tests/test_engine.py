"""Level-2 engine state-machine tests (testing.md §3) against a real hass.

Drives the DeviceEngine through its lifecycle with deterministic time-travel
(`async_fire_time_changed`) instead of the flaky live dev runtime: happy path,
max-attempts → escalated, auto-off → escalated, manual recover, cooldown →
suspect, and persistence/restore. A stub health + driver stand in for the real
sources so the *engine* logic is what's under test.

    PYTHONPATH=<ha-core>:<ha-core>/config python tests/test_engine.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta

import homeassistant.helpers.issue_registry as ir
from homeassistant.util import dt as dt_util

from tests.common import async_fire_time_changed, async_test_home_assistant

from custom_components.necromancer import _reconcile_issues, async_remove_entry
from custom_components.necromancer.core.drivers.base import RecoveryDriver
from custom_components.necromancer.core.engine import DeviceEngine, GState
from custom_components.necromancer.core.health.base import Health, HealthSource
from custom_components.necromancer.core.health.entity_state import EntityStateHealth
from custom_components.necromancer.core.health.template import TemplateHealth
from custom_components.necromancer.core.poe import PoeFabric
from custom_components.necromancer.core.policies.notify import NotifyPolicy
from custom_components.necromancer.core.policies.standard import StandardPolicy


class FakeHealth(HealthSource):
    def __init__(self, hass, verdict=Health.OK):
        super().__init__(hass, {})
        self.verdict = verdict

    @property
    def watched_entities(self):
        return []

    def evaluate(self):
        return self.verdict

    async def async_setup(self, on_change):
        return None


class StubDriver(RecoveryDriver):
    def __init__(self, hass, on_recover=None):
        super().__init__(hass, {"type": "stub"})
        self.calls = 0
        self.on_recover = on_recover
        self.raise_it = False
        self.result = True  # driver verdict returned by recover()

    async def can_recover(self):
        return True, ""

    async def recover(self, variables=None):
        self.calls += 1
        if self.raise_it:
            raise RuntimeError("boom")
        if self.on_recover:
            self.on_recover()
        return self.result


def make(hass, health, driver, **behavior):
    b = {
        "debounce": 30,
        "boot_window": 30,
        "cooldown": 30,
        "max_attempts": 2,
        "health_check": True,
    }
    b.update(behavior)
    return DeviceEngine(
        hass, "G", health, driver, StandardPolicy({}), b, subentry_id="g", engines={}
    )


def make_pair(hass, h1, d1, h2, d2, **behavior):
    """Two mutually-linked recover guards sharing one engines registry."""
    b = {
        "debounce": 30,
        "boot_window": 30,
        "cooldown": 30,
        "max_attempts": 2,
        "health_check": True,
    }
    b.update(behavior)
    engines: dict[str, DeviceEngine] = {}
    e1 = DeviceEngine(
        hass,
        "G1",
        h1,
        d1,
        StandardPolicy({}),
        dict(b),
        subentry_id="g1",
        linked_guards=["g2"],
        engines=engines,
    )
    e2 = DeviceEngine(
        hass,
        "G2",
        h2,
        d2,
        StandardPolicy({}),
        dict(b),
        subentry_id="g2",
        linked_guards=["g1"],
        engines=engines,
    )
    engines["g1"], engines["g2"] = e1, e2
    return e1, e2


async def _advance(hass, seconds):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


# ---------------- state machine ----------------


async def test_happy_path(hass, _):
    health = FakeHealth(hass, Health.OK)
    driver = StubDriver(hass, on_recover=lambda: setattr(health, "verdict", Health.OK))
    eng = make(hass, health, driver)
    await eng.async_start()
    assert eng.state is GState.OK
    health.verdict = Health.UNHEALTHY
    eng._evaluate()
    assert eng.state is GState.SUSPECT
    await _advance(hass, 30)  # debounce -> recover -> verify(OK) -> cooldown
    assert eng.state is GState.COOLDOWN, eng.state
    assert eng.recover_count == 1 and driver.calls == 1
    await _advance(hass, 30)  # cooldown -> ok (healthy)
    assert eng.state is GState.OK
    await eng.async_stop()


async def test_max_attempts_escalates(hass, _):
    health = FakeHealth(hass, Health.UNHEALTHY)
    driver = StubDriver(hass)  # recover() never fixes health
    eng = make(hass, health, driver, boot_window=0, max_attempts=2)
    await eng.async_start()  # already unhealthy -> suspect
    assert eng.state is GState.SUSPECT
    await _advance(hass, 30)  # debounce -> 2 attempts, both verify-timeout -> escalated
    assert eng.state is GState.ESCALATED, eng.state
    assert driver.calls == 2
    await eng.async_stop()


async def test_raising_driver_is_failed_attempt(hass, _):
    health = FakeHealth(hass, Health.UNHEALTHY)
    driver = StubDriver(hass)
    driver.raise_it = True
    eng = make(hass, health, driver, boot_window=0, max_attempts=1)
    await eng.async_start()
    await _advance(hass, 30)
    assert eng.state is GState.ESCALATED  # raise = failed, not false success
    await eng.async_stop()


async def test_health_check_off_driver_failure_escalates(hass, _):
    # Model B: with no health-check, a driver that reports failure is a failed
    # attempt (not a blind success) -> escalate + fail_count + driver result.
    health = FakeHealth(hass, Health.UNHEALTHY)
    driver = StubDriver(hass)
    driver.result = False  # driver runs but reports failure
    eng = make(hass, health, driver, health_check=False, boot_window=0, max_attempts=1)
    await eng.async_start()
    await _advance(hass, 30)  # debounce -> recover (driver failed) -> escalate
    assert eng.state is GState.ESCALATED, eng.state
    assert eng.fail_count == 1 and eng.last_fail is not None
    assert eng.last_recover_driver_result == "failed"
    await eng.async_stop()


async def test_health_check_off_driver_ok_succeeds(hass, _):
    # Model B: no health-check + driver reports good -> success (unchanged path).
    health = FakeHealth(hass, Health.UNHEALTHY)
    driver = StubDriver(hass)  # result True
    eng = make(hass, health, driver, health_check=False, max_attempts=1)
    await eng.async_start()
    await _advance(hass, 30)
    assert eng.state is GState.COOLDOWN
    assert eng.recover_count == 1 and eng.last_recover_driver_result == "good"
    await eng.async_stop()


async def test_fail_count_and_driver_good_on_verify_timeout(hass, _):
    # health-check on, driver runs clean but the device never returns: overall
    # failure (fail_count) yet the *driver* result is good (the diagnostic split).
    health = FakeHealth(hass, Health.UNHEALTHY)
    driver = StubDriver(hass)  # never fixes health -> verify times out
    eng = make(hass, health, driver, boot_window=0, max_attempts=1)
    await eng.async_start()
    await _advance(hass, 30)
    assert eng.state is GState.ESCALATED
    assert eng.fail_count == 1 and eng.last_fail is not None
    assert eng.last_recover_driver_result == "good"  # driver ran; device didn't return
    await eng.async_stop()


async def test_new_fields_persist(hass, _):
    health = FakeHealth(hass, Health.UNHEALTHY)
    driver = StubDriver(hass)
    eng = make(hass, health, driver, boot_window=0, max_attempts=1)
    await eng.async_start()
    await _advance(hass, 30)  # escalate -> fail_count, driver result recorded
    snap = eng.snapshot()
    eng2 = make(hass, FakeHealth(hass, Health.OK), StubDriver(hass))
    eng2._apply_persisted(snap)
    assert eng2.fail_count == 1 and eng2.last_fail is not None
    assert eng2.last_recover_driver_result == "good"
    await eng.async_stop()


async def test_auto_off_escalates(hass, _):
    health = FakeHealth(hass, Health.UNHEALTHY)
    driver = StubDriver(hass)
    eng = make(hass, health, driver)
    eng.auto = False
    await eng.async_start()
    await _advance(hass, 30)
    assert eng.state is GState.ESCALATED and driver.calls == 0
    await eng.async_stop()


async def test_manual_recover(hass, _):
    health = FakeHealth(hass, Health.UNHEALTHY)
    driver = StubDriver(hass, on_recover=lambda: setattr(health, "verdict", Health.OK))
    eng = make(hass, health, driver, boot_window=0)
    await eng.async_start()
    await eng.async_manual_recover()  # bypass debounce + auto gate
    await hass.async_block_till_done()
    assert driver.calls >= 1 and eng.state in (GState.COOLDOWN, GState.OK)
    await eng.async_stop()


async def test_manual_recover_ignored_while_busy(hass, _):
    # A press while a cycle is in flight must be ignored — not reset `attempt`
    # under the running loop (which would defeat max_attempts).
    health = FakeHealth(hass, Health.UNHEALTHY)
    gate = asyncio.Event()

    class BlockingDriver(StubDriver):
        async def recover(self, variables=None):
            self.calls += 1
            await gate.wait()  # hold the cycle in flight
            health.verdict = Health.OK

    driver = BlockingDriver(hass)
    eng = make(hass, health, driver, boot_window=0)
    await eng.async_start()
    await eng.async_manual_recover()  # spawns the cycle task
    await asyncio.sleep(0.05)  # let it run up to the blocked recover() (don't await it)
    assert eng.state is GState.RECOVERING and driver.calls == 1 and eng.attempt == 1
    await eng.async_manual_recover()  # busy → ignored
    await asyncio.sleep(0.05)
    assert driver.calls == 1 and eng.attempt == 1  # no second cycle, attempt intact
    gate.set()
    await hass.async_block_till_done()  # now the cycle can finish
    assert eng.state in (GState.COOLDOWN, GState.OK)
    await eng.async_stop()


async def test_manual_recover_during_snooze_lifts_snooze(hass, _):
    # A manual-recover press while snoozed must LIFT the snooze and recover — not
    # leave the guard snoozed-but-timerless, deaf to every future health change.
    health = FakeHealth(hass, Health.OK)
    driver = StubDriver(hass, on_recover=lambda: setattr(health, "verdict", Health.OK))
    eng = make(hass, health, driver, boot_window=0)
    await eng.async_start()
    eng.snooze(timedelta(seconds=300))
    assert eng.state is GState.SNOOZED
    await eng.async_manual_recover()
    await hass.async_block_till_done()
    assert eng._snoozed is False  # snooze lifted, not stranded
    await _advance(hass, 30)  # cooldown elapses -> OK (health is OK)
    assert eng.state is GState.OK
    health.verdict = Health.UNHEALTHY
    eng._evaluate()
    assert eng.state is GState.SUSPECT  # guard is live again, honoring health
    await eng.async_stop()


async def test_manual_recover_while_following_is_ignored(hass, _):
    # In follow-hold we have no cycle task yet, so _busy() is False — a manual
    # recover must still be ignored, not launch a competing cycle (the double
    # recovery linking exists to prevent).
    h2 = FakeHealth(hass, Health.UNHEALTHY)
    d2 = StubDriver(hass)
    _e1, e2 = make_pair(
        hass,
        FakeHealth(hass, Health.UNHEALTHY),
        StubDriver(hass),
        h2,
        d2,
        boot_window=0,
    )
    await e2.async_start()
    e2._on_partner_repair_start("g1")  # enter follow-hold (no cycle task yet)
    assert e2._following is True and not e2._busy()
    await e2.async_manual_recover()  # following -> must be ignored
    await hass.async_block_till_done()
    assert d2.calls == 0  # no competing own recovery ran
    await e2.async_stop()


async def test_cooldown_to_suspect(hass, _):
    health = FakeHealth(hass, Health.OK)
    driver = StubDriver(hass, on_recover=lambda: setattr(health, "verdict", Health.OK))
    eng = make(hass, health, driver)
    await eng.async_start()
    health.verdict = Health.UNHEALTHY
    eng._evaluate()
    await _advance(hass, 30)  # -> cooldown
    assert eng.state is GState.COOLDOWN
    health.verdict = Health.UNHEALTHY  # broke again during cooldown
    await _advance(hass, 30)  # cooldown elapsed, still unhealthy -> suspect
    assert eng.state is GState.SUSPECT, eng.state
    await eng.async_stop()


async def test_debounce_blip_absorbed(hass, _):
    health = FakeHealth(hass, Health.OK)
    driver = StubDriver(hass)
    eng = make(hass, health, driver)
    await eng.async_start()
    health.verdict = Health.UNHEALTHY
    eng._evaluate()
    assert eng.state is GState.SUSPECT
    health.verdict = Health.OK  # recovered before debounce elapsed
    await _advance(hass, 30)
    assert eng.state is GState.OK and driver.calls == 0
    await eng.async_stop()


# ---------------- guard linking + lifecycle ----------------


async def test_linked_follower_recovers_with_leader(hass, _):
    # Shared root cause: the leader's fix heals both. The follower holds (no own
    # cycle) and settles through the same success path (cooldown + a counted stat).
    h1 = FakeHealth(hass, Health.UNHEALTHY)
    h2 = FakeHealth(hass, Health.UNHEALTHY)

    def fix_both():
        h1.verdict = Health.OK
        h2.verdict = Health.OK

    d1 = StubDriver(hass, on_recover=fix_both)
    d2 = StubDriver(hass)
    e1, e2 = make_pair(hass, h1, d1, h2, d2)
    await e1.async_start()
    await e2.async_start()
    await e1.async_manual_recover()  # e1 leads, notifies g2 -> e2 follows
    await hass.async_block_till_done()
    assert e1.state is GState.COOLDOWN and e1.recover_count == 1
    assert e2.state is GState.COOLDOWN, e2.state  # follower validated healthy
    assert d2.calls == 0  # follower never launched its own recovery
    assert e2.recover_count == 1  # by design: a settled follower counts a recovery
    await e1.async_stop()
    await e2.async_stop()


async def test_linked_follower_escalates_when_leader_fails(hass, _):
    # Leader can't fix it and the follower is still down -> the follower escalates
    # (no cascade into a competing recovery that would re-trigger the group).
    h1 = FakeHealth(hass, Health.UNHEALTHY)
    h2 = FakeHealth(hass, Health.UNHEALTHY)
    d1 = StubDriver(hass)  # recover() never heals
    d2 = StubDriver(hass)
    e1, e2 = make_pair(hass, h1, d1, h2, d2, boot_window=0, max_attempts=1)
    await e1.async_start()
    await e2.async_start()
    await e1.async_manual_recover()
    await hass.async_block_till_done()
    assert e1.state is GState.ESCALATED
    assert e2.state is GState.ESCALATED, e2.state
    assert d2.calls == 0  # follower never ran a competing recovery
    await e1.async_stop()
    await e2.async_stop()


async def test_linked_auto_off_follower_escalates(hass, _):
    # Auto-off means off: the follower does not silently follow a group repair —
    # if its own device is affected it escalates instead.
    h1 = FakeHealth(hass, Health.UNHEALTHY)
    h2 = FakeHealth(hass, Health.UNHEALTHY)
    d1 = StubDriver(hass, on_recover=lambda: setattr(h1, "verdict", Health.OK))
    d2 = StubDriver(hass)
    e1, e2 = make_pair(hass, h1, d1, h2, d2)
    e2.auto = False
    await e1.async_start()
    await e2.async_start()
    await e1.async_manual_recover()
    await asyncio.sleep(0.05)  # let e1's cycle notify the partner
    assert e2.state is GState.ESCALATED, e2.state
    assert d2.calls == 0
    await hass.async_block_till_done()
    await e1.async_stop()
    await e2.async_stop()


async def test_debounce_arbitration_second_follows(hass, _):
    # Both trip together; the first to clear debounce leads (claims RECOVERING
    # synchronously) and the second follows instead of double-cycling.
    h1 = FakeHealth(hass, Health.UNHEALTHY)
    h2 = FakeHealth(hass, Health.UNHEALTHY)

    def fix_both():
        h1.verdict = Health.OK
        h2.verdict = Health.OK

    d1 = StubDriver(hass, on_recover=fix_both)
    d2 = StubDriver(hass, on_recover=fix_both)
    e1, e2 = make_pair(hass, h1, d1, h2, d2)
    await e1.async_start()
    await e2.async_start()
    assert e1.state is GState.SUSPECT and e2.state is GState.SUSPECT
    await _advance(hass, 30)  # both debounce -> one leads, one follows
    assert d1.calls + d2.calls == 1, (d1.calls, d2.calls)  # exactly one cycled
    assert e1.state is GState.COOLDOWN and e2.state is GState.COOLDOWN
    await e1.async_stop()
    await e2.async_stop()


async def test_validate_after_repair_blocks_manual_recover(hass, _):
    # B2: the follow-up verify is tracked as the cycle task, so a button press
    # while verifying is ignored (no second cycle, no _verify_event clobber).
    h2 = FakeHealth(hass, Health.UNHEALTHY)
    d2 = StubDriver(hass)
    e1, e2 = make_pair(
        hass, FakeHealth(hass, Health.OK), StubDriver(hass), h2, d2, boot_window=5
    )
    await e2.async_start()
    e2._on_partner_repair_start("g1")  # follow
    assert e2.state is GState.RECOVERING and e2._following is True
    e2._on_partner_repair_done("g1", True)  # schedule validate as the cycle task
    await asyncio.sleep(0.05)  # let validate reach its health wait
    assert e2._busy() and e2.state is GState.VERIFY
    await e2.async_manual_recover()  # busy -> ignored
    await asyncio.sleep(0.02)
    assert d2.calls == 0 and e2._busy()  # no competing cycle started
    h2.verdict = Health.OK
    e2._evaluate()  # fires the verify event
    await hass.async_block_till_done()
    assert e2.state is GState.COOLDOWN and e2.recover_count == 1 and d2.calls == 0
    await e1.async_stop()
    await e2.async_stop()


async def test_async_stop_cancels_validate_no_escalation(hass, _):
    # B2: stopping mid-verify cancels the validate cleanly — no terminal state set
    # on a torn-down engine, link state reset.
    h2 = FakeHealth(hass, Health.UNHEALTHY)
    e1, e2 = make_pair(
        hass,
        FakeHealth(hass, Health.OK),
        StubDriver(hass),
        h2,
        StubDriver(hass),
        boot_window=5,
    )
    await e2.async_start()
    e2._on_partner_repair_start("g1")
    e2._on_partner_repair_done("g1", True)
    await asyncio.sleep(0.05)
    assert e2._busy() and e2.state is GState.VERIFY
    await e2.async_stop()  # cancels the validate task
    await asyncio.sleep(0.05)
    assert e2.state is not GState.ESCALATED
    assert e2._following is False and e2._stopping is True
    assert not e2._busy()  # cycle slot cleared
    await hass.async_block_till_done()
    assert e2.state is not GState.ESCALATED  # no late mutation
    await e1.async_stop()


async def test_leader_stop_does_not_escalate_follower(hass, _):
    # B2: a reload/unload cancelling the leader mid-cycle must NOT fire a failed
    # repair to followers (which would escalate them off a half-finished cycle).
    gate = asyncio.Event()

    class BlockingDriver(StubDriver):
        async def recover(self, variables=None):
            self.calls += 1
            await gate.wait()  # hold the leader's cycle in flight

    h1 = FakeHealth(hass, Health.UNHEALTHY)
    h2 = FakeHealth(hass, Health.UNHEALTHY)
    e1, e2 = make_pair(
        hass, h1, BlockingDriver(hass), h2, StubDriver(hass), boot_window=5
    )
    await e1.async_start()
    await e2.async_start()
    await e1.async_manual_recover()
    await asyncio.sleep(0.05)  # e1 blocked in recover(); e2 following
    assert e1._busy() and e2._following is True and e2.state is GState.RECOVERING
    await e1.async_stop()  # cancel the leader mid-recover
    await asyncio.sleep(0.05)
    assert e2.state is not GState.ESCALATED  # finally skipped the partner notify
    assert e2._following is True  # never notified -> still holding
    gate.set()
    await e2.async_stop()
    assert e2._following is False
    await hass.async_block_till_done()


async def test_notify_only_guard_ignores_partner_start(hass, _):
    # A notify-only guard (allows_recovery False) is excluded from group repair.
    eng = DeviceEngine(
        hass,
        "N",
        FakeHealth(hass, Health.UNHEALTHY),
        StubDriver(hass),
        NotifyPolicy({}),
        {"debounce": 30},
        subentry_id="n",
        engines={},
    )
    eng._on_partner_repair_start("x")
    assert eng._following is False and eng.state is GState.OK
    await eng.async_stop()


async def test_partner_start_auto_off_but_healthy_no_escalation(hass, _):
    # Auto off + our own device is fine when a partner repairs: we neither follow
    # nor escalate — nothing is wrong with us.
    _e1, e2 = make_pair(
        hass,
        FakeHealth(hass, Health.OK),
        StubDriver(hass),
        FakeHealth(hass, Health.OK),
        StubDriver(hass),
    )
    e2.auto = False
    await e2.async_start()
    e2._on_partner_repair_start("g1")
    assert e2._following is False and e2.state is GState.OK
    await e2.async_stop()


async def test_partner_done_ignored_when_not_following(hass, _):
    # A done-notification we never followed (or from a different leader) is a no-op.
    _e1, e2 = make_pair(
        hass,
        FakeHealth(hass, Health.OK),
        StubDriver(hass),
        FakeHealth(hass, Health.OK),
        StubDriver(hass),
    )
    await e2.async_start()
    e2._on_partner_repair_done("g1", True)  # not following -> ignored
    assert e2.state is GState.OK and not e2._busy()
    e2._on_partner_repair_start("g1")  # now following g1
    e2._on_partner_repair_done("other", True)  # wrong leader -> ignored
    assert e2._following is True
    await e2.async_stop()


async def test_find_repairing_partner_skips_follower(hass, _):
    # A partner that is itself only FOLLOWING is not a leader to follow.
    e1, e2 = make_pair(
        hass,
        FakeHealth(hass, Health.UNHEALTHY),
        StubDriver(hass),
        FakeHealth(hass, Health.UNHEALTHY),
        StubDriver(hass),
    )
    await e1.async_start()
    await e2.async_start()
    e1._set_state(GState.RECOVERING)
    e1._following = True  # recovering, but only as a follower
    assert e2._find_repairing_partner() is None
    e1._following = False
    assert e2._find_repairing_partner() is e1  # now a genuine leader
    await e1.async_stop()
    await e2.async_stop()


async def test_validate_follower_still_down_does_own_recovery(hass, _):
    # Leader fixed its device but ours is still down -> fall back to our own
    # recovery (not escalation): the "still unhealthy, leader succeeded" branch.
    h2 = FakeHealth(hass, Health.UNHEALTHY)
    d2 = StubDriver(hass, on_recover=lambda: setattr(h2, "verdict", Health.OK))
    e1, e2 = make_pair(
        hass,
        FakeHealth(hass, Health.OK),
        StubDriver(hass),
        h2,
        d2,
        boot_window=0,
        debounce=1,
    )
    await e2.async_start()
    e2._on_partner_repair_start("g1")
    e2._on_partner_repair_done("g1", True)  # leader ok, but h2 still unhealthy
    await hass.async_block_till_done()  # validate -> OK -> _evaluate -> own SUSPECT
    assert e2.state is GState.SUSPECT, e2.state
    await _advance(hass, 1)  # own debounce -> recover -> verify ok -> cooldown
    assert d2.calls == 1 and e2.state is GState.COOLDOWN
    await e1.async_stop()
    await e2.async_stop()


async def test_suspect_clears_on_health_event(hass, _):
    # SUSPECT -> OK when health recovers via an event (not via the debounce timer).
    health = FakeHealth(hass, Health.OK)
    eng = make(hass, health, StubDriver(hass))
    await eng.async_start()
    health.verdict = Health.UNHEALTHY
    eng._evaluate()
    assert eng.state is GState.SUSPECT
    health.verdict = Health.OK
    eng._evaluate()  # health-event recovery cancels the timer + clears suspect
    assert eng.state is GState.OK
    await eng.async_stop()


async def test_following_guard_holds_on_health_change(hass, _):
    # While following, our own health dropping does NOT launch a competing cycle.
    h2 = FakeHealth(hass, Health.UNHEALTHY)
    _e1, e2 = make_pair(
        hass, FakeHealth(hass, Health.OK), StubDriver(hass), h2, StubDriver(hass)
    )
    await e2.async_start()
    e2._on_partner_repair_start("g1")  # follow -> RECOVERING, _following
    h2.verdict = Health.OK
    e2._evaluate()  # following -> just emit, no transition
    assert e2._following is True and e2.state is GState.RECOVERING
    await e2.async_stop()


async def test_notify_only_debounce_problem_detected(hass, _):
    # A notify-only guard reports a confirmed problem and goes ESCALATED.
    health = FakeHealth(hass, Health.UNHEALTHY)
    eng = DeviceEngine(
        hass,
        "N",
        health,
        StubDriver(hass),
        NotifyPolicy({}),
        {"debounce": 1},
        subentry_id="n",
        engines={},
    )
    await eng.async_start()
    assert eng.state is GState.SUSPECT
    await _advance(hass, 1)  # debounce -> observe -> problem_detected -> escalated
    assert eng.state is GState.ESCALATED
    await eng.async_stop()


async def test_raising_driver_retries_then_escalates(hass, _):
    # A raising recover() is a failed attempt: retry up to max, then escalate.
    health = FakeHealth(hass, Health.UNHEALTHY)
    driver = StubDriver(hass)
    driver.raise_it = True
    eng = make(hass, health, driver, boot_window=0, max_attempts=2)
    await eng.async_start()
    await _advance(hass, 30)
    assert eng.state is GState.ESCALATED and driver.calls == 2  # raised twice
    await eng.async_stop()


async def test_debounce_follows_recovering_partner(hass, _):
    # Arbitration via the debounce path: a partner already RECOVERING (that didn't
    # notify us) is picked up by _find_repairing_partner and we follow it.
    h2 = FakeHealth(hass, Health.UNHEALTHY)
    e1, e2 = make_pair(
        hass,
        FakeHealth(hass, Health.UNHEALTHY),
        StubDriver(hass),
        h2,
        StubDriver(hass),
        debounce=1,
    )
    await e2.async_start()  # e2 -> SUSPECT
    e1._set_state(GState.RECOVERING)  # e1 recovering, but never notified e2
    await _advance(hass, 1)  # e2 debounce -> finds repairing partner -> follows
    assert e2._following is True and e2.state is GState.RECOVERING
    assert e2.driver.calls == 0  # followed instead of cycling
    await e1.async_stop()
    await e2.async_stop()


async def test_plain_strategy_success_without_verify(hass, _):
    # A plain (no health-check) strategy counts success as soon as recover() runs.
    health = FakeHealth(hass, Health.UNHEALTHY)
    driver = StubDriver(hass)  # does NOT change health -> success is assumed
    eng = make(hass, health, driver, health_check=False)
    await eng.async_start()
    await _advance(hass, 30)  # debounce -> recover -> immediate success (no verify)
    assert eng.state is GState.COOLDOWN and eng.recover_count == 1 and driver.calls == 1
    await eng.async_stop()


async def test_malformed_timing_falls_back_to_default(hass, _):
    # A non-numeric behaviour value must not crash; _int falls back to the default.
    health = FakeHealth(hass, Health.OK)
    eng = make(hass, health, StubDriver(hass), debounce="not-a-number")
    await eng.async_start()
    health.verdict = Health.UNHEALTHY
    eng._evaluate()
    assert eng.state is GState.SUSPECT  # used the default debounce, no crash
    await eng.async_stop()


# ---------------- persistence ----------------


async def test_persistence_escalated_stays(hass, _):
    snap = {"state": "escalated", "attempt": 2, "recover_count": 3, "auto": False}
    health = FakeHealth(hass, Health.UNHEALTHY)
    eng = DeviceEngine(
        hass,
        "G",
        health,
        StubDriver(hass),
        StandardPolicy({}),
        {"debounce": 30},
        subentry_id="g",
        engines={},
        persisted=snap,
    )
    assert eng.state is GState.ESCALATED  # restored, no free retry
    assert eng.recover_count == 3 and eng.auto is False
    await eng.async_start()
    await hass.async_block_till_done()
    assert eng.state is GState.ESCALATED  # still unhealthy -> stays
    await eng.async_stop()


async def test_persistence_escalated_autoclears(hass, _):
    snap = {"state": "escalated", "attempt": 1, "recover_count": 1, "auto": True}
    health = FakeHealth(hass, Health.OK)  # healthy again at restart
    eng = DeviceEngine(
        hass,
        "G",
        health,
        StubDriver(hass),
        StandardPolicy({}),
        {"debounce": 30},
        subentry_id="g",
        engines={},
        persisted=snap,
    )
    assert eng.state is GState.ESCALATED
    await eng.async_start()  # _evaluate: escalated + healthy -> ok
    await hass.async_block_till_done()
    assert eng.state is GState.OK
    await eng.async_stop()


async def test_snapshot_roundtrip(hass, _):
    health = FakeHealth(hass, Health.OK)
    eng = make(hass, health, StubDriver(hass))
    eng.recover_count = 5
    eng.auto = False
    snap = eng.snapshot()
    assert snap["recover_count"] == 5 and snap["auto"] is False
    assert "resolved_port" not in snap  # dropped in the fabric refactor (H1b)
    await eng.async_stop()


async def test_reload_device_entry_on_repair(hass, _):
    from homeassistant.helpers import device_registry as dr

    from tests.common import MockConfigEntry

    entry = MockConfigEntry(domain="demo")
    entry.add_to_hass(hass)
    # Identifier tied to the (per-run unique) entry_id so the shared/persisted test
    # device registry can't accumulate config entries on a fixed device across runs.
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("demo", entry.entry_id)}
    )
    reloaded: list[str] = []

    async def _fake_reload(eid):
        reloaded.append(eid)

    orig = hass.config_entries.async_reload
    hass.config_entries.async_reload = _fake_reload
    try:
        eng = DeviceEngine(
            hass,
            "RL",
            FakeHealth(hass),
            StubDriver(hass),
            StandardPolicy({}),
            {"reload_entry": True, "reload_delay": 0},
            link_device_id=dev.id,
            subentry_id="rl",
            engines={},
        )
        await eng._maybe_reload_device_entry()
        assert reloaded == [entry.entry_id], reloaded
        # flag off -> no reload
        reloaded.clear()
        eng2 = DeviceEngine(
            hass,
            "RL2",
            FakeHealth(hass),
            StubDriver(hass),
            StandardPolicy({}),
            {},
            link_device_id=dev.id,
            subentry_id="rl2",
            engines={},
        )
        await eng2._maybe_reload_device_entry()
        assert reloaded == [], reloaded
    finally:
        hass.config_entries.async_reload = orig


async def test_escalate_blocked_no_recovered_error(hass, _):
    import logging

    records: list[tuple[int, str]] = []

    class _Cap(logging.Handler):
        def emit(self, record):
            records.append((record.levelno, record.getMessage()))

    cap = _Cap()
    logger = logging.getLogger("custom_components.necromancer")
    logger.addHandler(cap)
    try:
        eng = make(hass, FakeHealth(hass), StubDriver(hass))
        # blocked (pre-flight refusal): no "could not be recovered after N" error
        eng._escalate("recovery_blocked", reason="no port matches")
        await hass.async_block_till_done()
        assert not any("could not be recovered" in m for _, m in records), records
        # genuine failure: ERROR with the give-up message
        records.clear()
        eng._escalate()
        await hass.async_block_till_done()
        assert any(
            lvl == logging.ERROR and "could not be recovered" in m for lvl, m in records
        ), records
    finally:
        logger.removeHandler(cap)


async def test_raising_driver_warns_on_retry_traces_on_final(hass, _):
    import logging

    records: list[logging.LogRecord] = []

    class _Cap(logging.Handler):
        def emit(self, record):
            records.append(record)

    cap = _Cap()
    logger = logging.getLogger("custom_components.necromancer")
    logger.addHandler(cap)
    eng = None
    try:
        driver = StubDriver(hass)
        driver.raise_it = True
        eng = make(
            hass,
            FakeHealth(hass, Health.UNHEALTHY),
            driver,
            boot_window=0,
            max_attempts=2,
        )
        await eng.async_start()
        await _advance(hass, 60)  # debounce -> attempt 1 (retry) -> attempt 2 (final)
        assert eng.state is GState.ESCALATED, eng.state
        msgs = [(r.levelno, r.getMessage(), r.exc_info is not None) for r in records]
        # non-final attempt: WARNING, no traceback
        retry = [
            m for m in msgs if m[0] == logging.WARNING and "attempt 1/2 failed" in m[1]
        ]
        assert retry and retry[0][2] is False, msgs
        # final attempt: ERROR with traceback
        final = [m for m in msgs if "Recovery driver failed" in m[1]]
        assert final and final[0][0] == logging.ERROR and final[0][2] is True, msgs
    finally:
        logger.removeHandler(cap)
        if eng:
            await eng.async_stop()


async def test_follower_success_notify_gated(hass, _):
    notified: list[str] = []

    async def _rec(key, **kw):
        notified.append(key)

    # follower (via_link) + flag off (default) -> silent on success
    eng = make(hass, FakeHealth(hass), StubDriver(hass))
    eng._notify = _rec
    eng._recover_success(via_link=True)
    await hass.async_block_till_done()
    assert "recovery_success" not in notified, notified

    # follower (via_link) + flag on -> notifies
    notified.clear()
    eng2 = make(hass, FakeHealth(hass), StubDriver(hass), notify_follower_success=True)
    eng2._notify = _rec
    eng2._recover_success(via_link=True)
    await hass.async_block_till_done()
    assert "recovery_success" in notified, notified

    # leader / independent recovery (not via_link) -> always notifies
    notified.clear()
    eng3 = make(hass, FakeHealth(hass), StubDriver(hass))
    eng3._notify = _rec
    eng3._recover_success()
    await hass.async_block_till_done()
    assert "recovery_success" in notified, notified


async def test_ok_to_blind_on_unknown(hass, _):
    # Health unknown while monitoring -> a distinct `blind` status (not a stale ok),
    # and no recovery (unknown is never a fault).
    health = FakeHealth(hass, Health.OK)
    driver = StubDriver(hass)
    eng = make(hass, health, driver)
    await eng.async_start()
    assert eng.state is GState.OK
    health.verdict = Health.UNKNOWN
    eng._evaluate()
    assert eng.state is GState.BLIND
    assert driver.calls == 0
    await eng.async_stop()


async def test_blind_back_to_ok_on_healthy(hass, _):
    health = FakeHealth(hass, Health.UNKNOWN)
    eng = make(hass, health, StubDriver(hass))
    await eng.async_start()
    assert eng.state is GState.BLIND  # starts blind: health unknown
    health.verdict = Health.OK
    eng._evaluate()
    assert eng.state is GState.OK
    await eng.async_stop()


async def test_blind_to_suspect_on_unhealthy(hass, _):
    health = FakeHealth(hass, Health.UNKNOWN)
    eng = make(hass, health, StubDriver(hass))
    await eng.async_start()
    assert eng.state is GState.BLIND
    health.verdict = Health.UNHEALTHY
    eng._evaluate()
    assert eng.state is GState.SUSPECT  # re-detected unhealthy -> debounce -> recovery
    await eng.async_stop()


async def test_unknown_during_recovery_holds(hass, _):
    # Mid-recovery the device reads unknown (rebooting) -> hold, never blind.
    health = FakeHealth(hass, Health.UNKNOWN)
    eng = make(hass, health, StubDriver(hass))
    await eng.async_start()
    eng._set_state(GState.RECOVERING)
    eng._evaluate()
    assert eng.state is GState.RECOVERING
    await eng.async_stop()


async def test_template_missing_entity_warns_not_blind(hass, _):
    # A template that DEFAULTS a missing entity renders a concrete verdict, so the
    # guard isn't blind — warn about the missing entity, don't claim "blind".
    health = TemplateHealth(
        hass, {"template": "{{ states('sensor.gone')|float(0) > 150 }}"}
    )
    eng = make(hass, health, StubDriver(hass))
    keys = {p["key"] for p in eng.config_problems()}
    assert "health_template_missing_entity" in keys
    assert "health_template_blind" not in keys


async def test_template_truly_blind(hass, _):
    # A template that yields unknown (no usable value) is genuinely blind.
    health = TemplateHealth(hass, {"template": "{{ states('sensor.gone') }}"})
    eng = make(hass, health, StubDriver(hass))
    keys = {p["key"] for p in eng.config_problems()}
    assert "health_template_blind" in keys
    assert "health_template_missing_entity" not in keys


async def test_reconcile_creates_and_clears_config_issue(hass, _):
    # A guard whose health entity doesn't exist -> blind -> a repair issue; once
    # the entity exists, re-reconciling clears it (self-healing on reload).
    health = EntityStateHealth(hass, {"entity_id": "sensor.ghost"})
    eng = make(hass, health, StubDriver(hass))
    fabric = PoeFabric(hass)
    _reconcile_issues(hass, {"g": eng}, fabric)
    assert ("necromancer", "g_health_entity_missing") in ir.async_get(hass).issues
    hass.states.async_set("sensor.ghost", "on")  # entity exists now -> fixed
    _reconcile_issues(hass, {"g": eng}, fabric)
    assert ("necromancer", "g_health_entity_missing") not in ir.async_get(hass).issues


async def test_reconcile_clears_issue_for_removed_guard(hass, _):
    # Deleting a guard subentry (its engine gone) must clear its stale repair issue.
    health = EntityStateHealth(hass, {"entity_id": "sensor.ghost2"})
    eng = make(hass, health, StubDriver(hass))
    fabric = PoeFabric(hass)
    _reconcile_issues(hass, {"g": eng}, fabric)
    assert ("necromancer", "g_health_entity_missing") in ir.async_get(hass).issues
    _reconcile_issues(hass, {}, fabric)  # subentry deleted -> no engines
    assert ("necromancer", "g_health_entity_missing") not in ir.async_get(hass).issues


async def test_remove_entry_clears_issues(hass, _):
    # Deleting the whole integration must drop its repair issues immediately.
    health = EntityStateHealth(hass, {"entity_id": "sensor.ghost3"})
    eng = make(hass, health, StubDriver(hass))
    _reconcile_issues(hass, {"g": eng}, PoeFabric(hass))
    assert ("necromancer", "g_health_entity_missing") in ir.async_get(hass).issues
    await async_remove_entry(hass, None)  # entry unused by the cleanup
    assert ("necromancer", "g_health_entity_missing") not in ir.async_get(hass).issues


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


async def main() -> int:
    passed, failed = 0, 0
    async with async_test_home_assistant() as hass:
        for t in TESTS:
            try:
                await t(hass, None)
            except Exception as err:  # noqa: BLE001
                failed += 1
                print(f"FAIL  {t.__name__}: {err!r}")
            else:
                passed += 1
                print(f"ok    {t.__name__}")
        await hass.async_stop()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
