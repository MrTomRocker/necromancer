"""Base classes for Necromancer recovery drivers.

A RecoveryDriver performs the actual repair: power-cycle a switch
(`switch_cycle`), run one or two user-defined action sequences (`action_call` /
`action_cycle`), or auto-resolve a device to its PoE port (`poe_port`). Whether
the result is verified against the device's health entity is the engine's job.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from homeassistant.core import HomeAssistant


class RecoveryDriver(ABC):
    """Performs a recovery action for a guarded device."""

    def __init__(self, hass: HomeAssistant, config: dict) -> None:
        self.hass = hass
        self.config = config
        # Optional persistent cache, wired by the engine via `bind_cache`: lets a
        # driver remember a resolved target across restarts. Only `poe_port` uses
        # it (last-known port, so a device that has aged out of the switch's
        # neighbour table while down can still be recovered).
        self._cache_get: Callable[[], str | None] | None = None
        self._cache_set: Callable[[str | None], None] | None = None

    def bind_cache(
        self,
        get: Callable[[], str | None],
        set_: Callable[[str | None], None],
    ) -> None:
        """Wire a persistent get/set for the driver's resolved target."""
        self._cache_get = get
        self._cache_set = set_

    def observe(self) -> None:  # noqa: B027
        """Optional: learn the current target while the device is healthy.

        Called by the engine on every healthy evaluation; a driver that resolves
        its target dynamically (poe_port) refreshes its cache here so a fallback
        is available later when live resolution fails.
        """

    async def async_setup(self) -> Callable[[], None] | None:
        """Optional: start watching what affects resolution; return an unsub.

        `poe_port` uses this to refresh its cached port whenever a port's
        id-entity changes — the switch's neighbour table updates independently of
        the device's health entity, so observing only on health events would miss
        it. Default: nothing to watch.
        """
        return None

    async def can_recover(self) -> tuple[bool, str]:
        """Guard right before recovering. Returns (allowed, reason)."""
        return True, ""

    @abstractmethod
    async def recover(self) -> None:
        """Perform the recovery. Should return once the action is done."""

    def target_info(self) -> str:
        """Short human description of the target (e.g. the service or port)."""
        return self.config.get("type", "")

    def config_errors(self) -> list[str]:
        """Return human-readable config errors (e.g. a missing service).

        Checked at startup and logged at ERROR. Empty list = all good.
        """
        return []
