"""Necromancer domain core: the self-healing engine and its pluggable parts.

Framework logic decoupled from the Home Assistant shell (platforms, config flow,
entity glue stay at the package root). Layering: HealthSource -> Engine(Policy) ->
RecoveryDriver, with LinkCoordinator grouping and the PoE fabric.
"""
