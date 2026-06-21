"""Recovery driver: auto-resolve a device to its PoE port, then power-cycle it.

`poe_port` is a thin strategy adapter over the shared **PoE fabric** (`core/poe.py`):
the guard carries an `expected_id` (MAC, hostname, neighbour, or a static label),
and the fabric owns the port list, the id→port resolution (live match → last-known
cache → ambiguous/none), the staged power-cycle, the per-port lock and status. So
a `poe_port` guard and the `necromancer.repair_poe_port` service share *one* cycle
and *one* cache for a given port — they never fight over it.

`can_recover` blocks (→ engine escalates, no blind cycling) when the id can't be
resolved to exactly one port. Verifying the *device* is healthy again afterwards is
the engine's own VERIFY step (boot_window); the fabric only confirms the port came
back (its staged verify).
"""

from __future__ import annotations

from ...const import CONF_EXPECTED_ID, DOMAIN, LOGGER
from .base import RecoveryDriver


class PoePortDriver(RecoveryDriver):
    """Resolve `expected_id` to a port via the shared fabric, then cycle it."""

    @property
    def expected_id(self) -> str:
        return self.config[CONF_EXPECTED_ID]

    def _fabric(self):
        """The domain-singleton PoE fabric (created in async_setup_entry)."""
        return self.hass.data.get(DOMAIN, {}).get("fabric")

    async def can_recover(self) -> tuple[bool, str]:
        fabric = self._fabric()
        if fabric is None:
            return False, "PoE fabric not ready"
        port, reason = fabric.resolve_with_reason(self.expected_id)
        if port is None:
            LOGGER.error("PoE %s: %s", self.expected_id, reason)
            return False, reason
        return True, ""

    async def recover(self) -> None:
        fabric = self._fabric()
        if fabric is None:
            LOGGER.error("PoE %s: fabric not available", self.expected_id)
            return
        # The fabric resolves, locks the port, cycles it and sets its status; the
        # engine's VERIFY step then checks the device's own health.
        await fabric.repair(self.expected_id)

    def target_info(self) -> str:
        fabric = self._fabric()
        if fabric is None:
            return f"id={self.expected_id}"
        return fabric.target_info(self.expected_id)

    def config_errors(self) -> list[str]:
        fabric = self._fabric()
        if fabric is None or fabric.port_count == 0:
            return [
                f"poe_port '{self.expected_id}': no ports configured — "
                "add ports in the integration's options"
            ]
        return []
