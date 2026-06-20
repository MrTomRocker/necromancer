"""Guard-link grouping: connected components over symmetric guard links.

Each guard declares a set of partner subentry_ids. The relation is treated as
undirected and unioned, then expanded to connected components — so linking A-B
where B-C already exists puts {A, B, C} in one group (clique closure). Both the
runtime (engine partners) and the config flow (form defaults, unlink diff) read
the group through here, so a one-sided declaration still shows and behaves as a
full mutual group; only an explicit unlink (clearing the edge on both sides)
splits it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from .const import CONF_BOOT_WINDOW, DEFAULT_BOOT_WINDOW, EVENT_GUARD_REPAIR, LOGGER
from .health import Health
from .state import GState

if TYPE_CHECKING:
    from .engine import DeviceEngine


def link_components(links: dict[str, set[str]], valid: set[str]) -> dict[str, set[str]]:
    """Map each guard to its full group (clique-closed, incl. itself).

    `links` is each guard's declared partner ids; `valid` is the set of existing
    guards (stale ids are dropped).
    """
    adj: dict[str, set[str]] = {guard: set() for guard in valid}
    for guard, partners in links.items():
        if guard not in valid:
            continue
        for partner in partners:
            if partner in valid and partner != guard:
                adj[guard].add(partner)
                adj[partner].add(guard)
    comp: dict[str, set[str]] = {}
    seen: set[str] = set()
    for start in valid:
        if start in seen:
            continue
        group: set[str] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            if node in group:
                continue
            group.add(node)
            stack.extend(adj[node] - group)
        seen |= group
        for member in group:
            comp[member] = group
    return comp


def group_of(links: dict[str, set[str]], valid: set[str], guard: str) -> set[str]:
    """The group partners of one guard (clique-closed, excluding itself)."""
    return link_components(links, valid).get(guard, {guard}) - {guard}


class LinkCoordinator:
    """Runtime guard-link coordination for one DeviceEngine.

    Owns the guard's group membership and leader/follower state, and runs the
    partner hand-off (start -> hold -> verify). Peers are reached through their own
    coordinators (``engine.links``), so engines never touch each other's internals.
    The state transitions themselves stay in the engine; the coordinator drives
    them through its single engine reference.
    """

    def __init__(
        self,
        engine: DeviceEngine,
        linked: list[str] | None,
        engines: dict[str, DeviceEngine] | None,
    ) -> None:
        self._engine = engine
        self._linked = linked or []
        self._engines = engines if engines is not None else {}
        self.following = False
        self.leader: str | None = None

    def reset(self) -> None:
        """Drop follower state (on engine teardown)."""
        self.following = False
        self.leader = None

    def _partners(self) -> Iterator[DeviceEngine]:
        """Yield the live engines of our group partners (skipping ourselves)."""
        for pid in self._linked:
            partner = self._engines.get(pid)
            if partner is not None and partner is not self._engine:
                yield partner

    def find_repairing_partner(self) -> DeviceEngine | None:
        """A group partner that is actively repairing (the leader to follow)."""
        for partner in self._partners():
            if partner.state in (GState.RECOVERING, GState.VERIFY) and not (
                partner.links.following
            ):
                return partner
        return None

    def notify_start(self) -> None:
        """Tell the group we are repairing so partners hold instead of competing."""
        eng = self._engine
        if eng._subentry_id is None:
            return
        eng.hass.bus.async_fire(
            EVENT_GUARD_REPAIR,
            {"guard": eng._subentry_id, "name": eng.name, "phase": "start"},
        )
        for partner in self._partners():
            partner.links.on_partner_repair_start(eng._subentry_id)

    def notify_done(self, success: bool) -> None:
        """Tell the group our repair finished; the verdict steers the followers."""
        eng = self._engine
        if eng._subentry_id is None:
            return
        eng.hass.bus.async_fire(
            EVENT_GUARD_REPAIR,
            {
                "guard": eng._subentry_id,
                "name": eng.name,
                "phase": "done",
                "success": success,
            },
        )
        for partner in self._partners():
            partner.links.on_partner_repair_done(eng._subentry_id, success)

    def on_partner_repair_start(self, leader_id: str) -> None:
        """A linked guard started repairing.

        Normally we *follow* (hold, then re-verify) instead of launching a competing
        recovery. But auto-recovery off means off: a guard with its auto switch
        disabled never participates in a group repair — if its own device is actually
        affected it escalates (alarm) rather than silently following someone else's
        fix.
        """
        eng = self._engine
        if not eng.allows_recovery or eng._busy() or self.following:
            return
        if not eng.auto:
            if eng.health.evaluate() == Health.UNHEALTHY and eng.state != (
                GState.ESCALATED
            ):
                LOGGER.warning(
                    "%s: linked guard repairing but auto-recovery is off — escalating",
                    eng.name,
                )
                eng._cancel_timer()
                eng._set_state(GState.ESCALATED)
                eng.hass.async_create_task(
                    eng._notify("no_auto_recovery", reason="auto_off")
                )
            return
        LOGGER.info(
            "%s: linked guard is repairing — following (hold, verify after)", eng.name
        )
        self.following = True
        self.leader = leader_id
        eng._cancel_timer()
        eng._set_state(GState.RECOVERING)

    def on_partner_repair_done(self, leader_id: str, success: bool) -> None:
        """Our leader finished — re-validate; the verdict steers the fallback."""
        eng = self._engine
        if not self.following or self.leader != leader_id:
            return
        self.following = False
        self.leader = None
        if eng._busy() or eng._stopping:
            return
        # Track the follow-up verify as the engine's cycle task so the busy-guard
        # and async_stop cover it: a manual recover or a fresh partner-start can't
        # race a second cycle onto us, and a reload cancels it cleanly.
        eng._cycle_task = eng.hass.async_create_task(
            self.validate_after_repair(leader_success=success)
        )

    async def validate_after_repair(self, *, leader_success: bool) -> None:
        """Re-check health after a group repair.

        - Healthy → the follower's device was recovered by the shared fix, so it
          settles through the same success path as the leader (COOLDOWN + stats),
          instead of snapping straight back to OK.
        - Still unhealthy, leader **succeeded** → only *our* device is still down, so
          fall back to our own recovery.
        - Still unhealthy, leader **failed** → the shared cause is unfixed; don't
          cascade into a competing recovery (which would just re-trigger the group) —
          follow the leader's escalation instead.
        """
        eng = self._engine
        try:
            eng._set_state(GState.VERIFY)
            if await eng._wait_health_ok(
                eng._int(CONF_BOOT_WINDOW, DEFAULT_BOOT_WINDOW)
            ):
                LOGGER.info("%s: healthy after linked-guard repair", eng.name)
                eng._recover_success()
            elif leader_success:
                LOGGER.info(
                    "%s: still unhealthy after linked repair — own recovery", eng.name
                )
                eng._set_state(GState.OK)
                eng._evaluate()
            else:
                LOGGER.warning(
                    "%s: linked repair failed and still unhealthy — escalating",
                    eng.name,
                )
                eng._set_state(GState.ESCALATED)
                eng.hass.async_create_task(eng._notify("linked_repair_failed"))
        finally:
            # Clear the cycle slot like _run_recovery_cycle does, so a later
            # suspect/manual recover can start a fresh cycle (a cancelled verify
            # unwinds here too, leaving no work on a torn-down engine).
            eng._cycle_task = None
