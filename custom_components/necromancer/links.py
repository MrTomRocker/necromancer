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
