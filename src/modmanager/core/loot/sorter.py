"""LOOT's sorting algorithm.

Follows https://loot-api.readthedocs.io/en/latest/api/sorting.html:

1. Masters and non-masters are sorted in separate graphs, then concatenated.
2. Hard edges: a plugin loads after its masters, its requirements and its
   "load after" files. A cycle among these is an error.
3. Group edges: plugins load after the plugins of every group their group is
   (transitively) after, unless that would create a cycle. Plugins in the
   ``default`` group only get these edges in a final pass.
4. Overlap edges: when two plugins contain the same record, the one that
   overrides more records loads first, unless that would create a cycle.
5. Tie-breaks keep the current load order wherever the edges allow. LOOT pins
   moved plugins as late as possible; here the same effect comes from a reverse
   topological sort that always places the latest-positioned eligible plugin last.

Asset (BSA) overlap, LOOT's last tie-breaker between overlapping plugins with
equal override counts, is not implemented.
"""

from __future__ import annotations

import heapq
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from modmanager.core.loot import conditions
from modmanager.core.loot.masterlist import Metadata, PluginMeta
from modmanager.core.loot.records import PluginRecords

log = logging.getLogger(__name__)


class CycleError(Exception):
    def __init__(self, cycle: list[str], kinds: list[str]):
        self.cycle = cycle
        steps = " → ".join(f"{a} ({k})" for a, k in zip(cycle, kinds)) + f" → {cycle[0]}"
        super().__init__(f"The plugins' masters and LOOT rules form a cycle: {steps}")


@dataclass
class SortPlugin:
    name: str
    is_master: bool
    masters: list[str] = field(default_factory=list)
    meta: PluginMeta = field(default_factory=PluginMeta)
    records: PluginRecords | None = None


@dataclass
class SortResult:
    order: list[str]
    moved: int = 0
    notes: list[str] = field(default_factory=list)


class Graph:
    def __init__(self, plugins: list[SortPlugin]):
        self.plugins = plugins
        self.index = {p.name.lower(): i for i, p in enumerate(plugins)}
        self.out: list[set[int]] = [set() for _ in plugins]
        self.kind: dict[tuple[int, int], str] = {}

    def add(self, a: int, b: int, kind: str) -> None:
        if a != b and b not in self.out[a]:
            self.out[a].add(b)
            self.kind[(a, b)] = kind

    def reach(self, start: int) -> set[int]:
        seen = {start}
        stack = [start]
        while stack:
            for nxt in self.out[stack.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    def add_unless_cycle(self, sources: list[int], target: int, kind: str) -> int:
        """Add source -> target edges that don't close a cycle. Adding edges into
        ``target`` never changes what ``target`` reaches, so one search suffices."""
        blocked = self.reach(target)
        added = 0
        for s in sources:
            if s not in blocked:
                if target not in self.out[s]:
                    added += 1
                self.add(s, target, kind)
        return added

    def find_cycle(self) -> list[int] | None:
        color = [0] * len(self.plugins)
        parent: dict[int, int] = {}
        for root in range(len(self.plugins)):
            if color[root]:
                continue
            stack = [(root, iter(sorted(self.out[root])))]
            color[root] = 1
            while stack:
                node, it = stack[-1]
                nxt = next(it, None)
                if nxt is None:
                    color[node] = 2
                    stack.pop()
                elif color[nxt] == 1:
                    cycle = [nxt]
                    cur = node
                    while cur != nxt:
                        cycle.append(cur)
                        cur = parent[cur]
                    return list(reversed(cycle))
                elif color[nxt] == 0:
                    color[nxt] = 1
                    parent[nxt] = node
                    stack.append((nxt, iter(sorted(self.out[nxt]))))
        return None


def sort_plugins(plugins: list[SortPlugin], metadata: Metadata, current: list[str],
                 ctx: conditions.Context | None = None,
                 progress: Callable[[str], None] | None = None) -> SortResult:
    """Sort ``plugins`` (excluding the game's hardcoded ones) and return the new order."""
    position = {n.lower(): i for i, n in enumerate(current)}
    ordered = sorted(plugins, key=lambda p: p.name.lower())
    masters = [p for p in ordered if p.is_master]
    others = [p for p in ordered if not p.is_master]
    notes: list[str] = []
    result: list[str] = []
    for label, group in (("masters", masters), ("plugins", others)):
        if progress:
            progress(f"Sorting {len(group)} {label}…")
        result += _sort_graph(group, metadata, position, ctx, notes)
    old = [n for n in current if n.lower() in {p.name.lower() for p in plugins}]
    moved = sum(1 for a, b in zip(old, result) if a.lower() != b.lower())
    return SortResult(result, moved, notes)


def _sort_graph(plugins: list[SortPlugin], metadata: Metadata, position: dict[str, int],
                ctx: conditions.Context | None, notes: list[str]) -> list[str]:
    if not plugins:
        return []
    g = Graph(plugins)

    def applies(ref) -> bool:
        if not ref.condition:
            return True
        return ctx is not None and conditions.evaluate(ref.condition, ctx) is True

    # Hard edges
    for i, p in enumerate(plugins):
        for m in p.masters:
            j = g.index.get(m.lower())
            if j is not None:
                g.add(j, i, "master")
        for kind, refs in (("requirement", p.meta.req), ("load after", p.meta.after)):
            for ref in refs:
                j = g.index.get(ref.name.lower())
                if j is not None and applies(ref):
                    g.add(j, i, kind)
    cycle = g.find_cycle()
    if cycle:
        kinds = [g.kind.get((cycle[k], cycle[(k + 1) % len(cycle)]), "?") for k in range(len(cycle))]
        raise CycleError([plugins[i].name for i in cycle], kinds)

    _group_edges(g, metadata, notes)
    _overlap_edges(g)
    return _tie_break(g, position)


def _group_edges(g: Graph, metadata: Metadata, notes: list[str]) -> None:
    members: dict[str, list[int]] = {}
    for i, p in enumerate(g.plugins):
        members.setdefault(p.meta.group or "default", []).append(i)
    succ: dict[str, list[str]] = {name: [] for name in metadata.group_order}
    indegree = {name: 0 for name in metadata.group_order}
    for name in metadata.group_order:
        for before in metadata.groups.get(name, []):
            if before in succ and name not in succ[before]:
                succ[before].append(name)
                indegree[name] += 1

    longest: dict[str, int] = {}

    def depth(name: str, visiting: frozenset = frozenset()) -> int:
        if name in longest:
            return longest[name]
        if name in visiting:
            return 0  # Cyclic groups: stop rather than recurse forever.
        value = 1 + max((depth(s, visiting | {name}) for s in succ[name]), default=0)
        longest[name] = value
        return value

    roots = sorted((n for n in metadata.group_order if indegree[n] == 0), key=lambda n: -depth(n))
    starts = roots + [n for n in metadata.group_order if indegree[n] != 0]

    def search(start: str, skip_default: bool) -> None:
        visited = {start}
        path: list[str] = []

        def visit(group: str) -> None:
            for target in succ[group]:
                path.append(group)
                targets = members.get(target, [])
                if targets:
                    sources = [i for grp in path if not (skip_default and grp == "default")
                               for i in members.get(grp, [])]
                    if sources:
                        for t in targets:
                            g.add_unless_cycle(sources, t, "group")
                if target not in visited:
                    visited.add(target)
                    visit(target)
                path.pop()

        visit(start)

    for start in starts:
        search(start, skip_default=True)
    if "default" in succ:
        search("default", skip_default=False)


def _overlap_edges(g: Graph) -> None:
    recs = [p.records for p in g.plugins]
    overridden: set[int] = set()
    for r in recs:
        if r is not None:
            overridden |= r.overrides
    holders: dict[int, list[int]] = {}
    pairs: set[tuple[int, int]] = set()
    for i, r in enumerate(recs):
        if r is None:
            continue
        for key in r.keys & overridden:
            lst = holders.setdefault(key, [])
            for j in lst:
                pairs.add((j, i))
            lst.append(i)
    incoming: dict[int, list[int]] = {}
    for a, b in pairs:
        ca, cb = recs[a].override_count, recs[b].override_count
        if ca == cb:
            continue
        src, dst = (a, b) if ca > cb else (b, a)
        incoming.setdefault(dst, []).append(src)
    for dst in sorted(incoming):
        g.add_unless_cycle(sorted(incoming[dst]), dst, "overlap")


def _tie_break(g: Graph, position: dict[str, int]) -> list[str]:
    def key(i: int):
        name = g.plugins[i].name.lower()
        if name in position:
            return (0, position[name], "", "")
        stem, _, ext = name.rpartition(".")
        return (1, 0, stem, ext)

    keys = [key(i) for i in range(len(g.plugins))]
    rank = {k: r for r, k in enumerate(sorted(set(keys)))}
    preds: list[set[int]] = [set() for _ in g.plugins]
    remaining_out = [len(o) for o in g.out]
    for a, outs in enumerate(g.out):
        for b in outs:
            preds[b].add(a)
    # Build the order from the end: repeatedly place the eligible plugin that
    # comes last in the current order, so plugins only move as far as they must.
    heap = [(-rank[keys[i]], i) for i in range(len(g.plugins)) if remaining_out[i] == 0]
    heapq.heapify(heap)
    reversed_order: list[int] = []
    while heap:
        _, i = heapq.heappop(heap)
        reversed_order.append(i)
        for p in preds[i]:
            remaining_out[p] -= 1
            if remaining_out[p] == 0:
                heapq.heappush(heap, (-rank[keys[p]], p))
    if len(reversed_order) != len(g.plugins):
        raise RuntimeError("Sorting produced a cycle; please report this as a bug.")
    return [g.plugins[i].name for i in reversed(reversed_order)]
