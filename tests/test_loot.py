import os
import time

import pytest
from conftest import add_mod, make_plugin

from modmanager.core.loot import conditions, masterlist, records
from modmanager.core.loot.masterlist import Metadata
from modmanager.core.loot.sorter import CycleError, SortPlugin, sort_plugins
from modmanager.core.manager import Manager

MASTERLIST = """
groups:
  - name: Early
  - name: default
    after: [Early]
  - name: Late
    after: [default]
  - name: Very Late
    after: [Late]
plugins:
  - name: 'Early.esp'
    group: Early
  - name: 'Bashed Patch, \\d+\\.esp'
    group: Very Late
  - name: 'Late.esp'
    group: Late
  - name: 'Needs.esp'
    req: ['Wanted.esp']
  - name: 'Follower.esp'
    after:
      - name: 'Leader.esp'
        condition: 'active("Leader.esp")'
  - name: 'Dirty.esp'
    dirty:
      - crc: 0x{crc}
        util: '[SSEEdit](https://example.com/sseedit)'
        itm: 3
        udr: 1
    msg:
      - type: warn
        content: 'Use {0} instead.'
        subs: ['Something Better']
      - type: error
        content: 'Only when X is active'
        condition: 'active("X.esp")'
"""


def P(name, master=False, masters=(), meta=None, recs=None):
    return SortPlugin(name, master, list(masters), meta or masterlist.PluginMeta(), recs)


def names(result):
    return result.order


def test_record_scan(tmp_path):
    base = make_plugin(tmp_path / "Base.esm", master=True, records=[0x000800, 0x000801])
    patch = make_plugin(tmp_path / "Patch.esp", masters=["Base.esm"], records=[0x00000800, 0x01000900])
    b, p = records.scan(base), records.scan(patch)
    assert b.total == 2 and b.override_count == 0
    assert p.total == 2 and p.override_count == 1
    assert b.keys & p.keys == p.overrides  # Patch overrides Base's 0x800


def test_conditions(tmp_path):
    (tmp_path / "Data").mkdir()
    ctx = conditions.Context(
        data_dir=tmp_path / "Data", game_dir=tmp_path, present={"a.esp", "b.esp", "skse/plugins/x.dll"},
        active={"a.esp"}, masters={"b.esp"},
    )
    ev = lambda c: conditions.evaluate(c, ctx)
    assert ev('active("A.esp")') is True
    assert ev('active("B.esp")') is False
    assert ev('file("B.esp") and not active("B.esp")') is True
    assert ev('file("SKSE/Plugins/X.dll")') is True
    assert ev('many("[ab]\\.esp")') is True
    assert ev('active("Z.esp") or (is_master("b.esp") and file("a.esp"))') is True
    assert ev('version("Missing.esp", "1.0", <)') is True  # missing files count as oldest
    assert ev('some_new_function("x")') is None
    assert ev(None) is True


def test_masters_and_requirements():
    meta = Metadata([{"plugins": [{"name": "Needs.esp", "req": ["Wanted.esp"]}]}])
    plugins = [
        P("Needs.esp", meta=meta.for_plugin("Needs.esp")),
        P("Child.esp", masters=["Parent.esp"]),
        P("Parent.esp"),
        P("Wanted.esp"),
    ]
    result = sort_plugins(plugins, meta, ["Needs.esp", "Child.esp", "Parent.esp", "Wanted.esp"])
    order = names(result)
    assert order.index("Parent.esp") < order.index("Child.esp")
    assert order.index("Wanted.esp") < order.index("Needs.esp")


def test_groups_and_regex_names():
    meta = Metadata([masterlist._yaml_load(MASTERLIST.replace("{crc}", "0"))])
    plugins = [P(n, meta=meta.for_plugin(n)) for n in ("Bashed Patch, 0.esp", "Late.esp", "Mod.esp", "Early.esp")]
    result = sort_plugins(plugins, meta, [p.name for p in plugins])
    assert names(result) == ["Early.esp", "Mod.esp", "Late.esp", "Bashed Patch, 0.esp"]


def test_group_edge_that_would_cycle_is_skipped():
    meta = Metadata([{"groups": [{"name": "default"}, {"name": "Late", "after": ["default"]}],
                      "plugins": [{"name": "Late.esp", "group": "Late"}]}])
    # Default.esp must load after Late.esp because Late.esp is its master: the group rule gives way.
    plugins = [P("Late.esp", meta=meta.for_plugin("Late.esp")), P("Default.esp", masters=["Late.esp"]), P("Other.esp")]
    order = names(sort_plugins(plugins, meta, ["Other.esp", "Late.esp", "Default.esp"]))
    assert order.index("Late.esp") < order.index("Default.esp")
    assert order.index("Other.esp") < order.index("Late.esp")


def test_conditional_load_after(tmp_path):
    meta = Metadata([masterlist._yaml_load(MASTERLIST.replace("{crc}", "0"))])
    plugins = [P(n, meta=meta.for_plugin(n)) for n in ("Follower.esp", "Leader.esp")]
    ctx = conditions.Context(data_dir=tmp_path, game_dir=tmp_path, present=set(), active=set())
    assert names(sort_plugins(plugins, meta, ["Follower.esp", "Leader.esp"], ctx)) == ["Follower.esp", "Leader.esp"]
    ctx.active = {"leader.esp"}
    assert names(sort_plugins(plugins, meta, ["Follower.esp", "Leader.esp"], ctx)) == ["Leader.esp", "Follower.esp"]


def test_overlap_puts_bigger_overrider_first(tmp_path):
    make_plugin(tmp_path / "Base.esm", master=True, records=[0x800, 0x801, 0x802])
    big = make_plugin(tmp_path / "Overhaul.esp", masters=["Base.esm"], records=[0x800, 0x801, 0x802])
    small = make_plugin(tmp_path / "Tweak.esp", masters=["Base.esm"], records=[0x801])
    alone = make_plugin(tmp_path / "Unrelated.esp", masters=["Base.esm"], records=[0x01000900])
    meta = Metadata([])
    plugins = [P(p.name, masters=["Base.esm"], recs=records.scan(p)) for p in (small, big, alone)]
    order = names(sort_plugins(plugins, meta, ["Tweak.esp", "Unrelated.esp", "Overhaul.esp"]))
    # Overhaul must precede Tweak, so it moves just in front of it; Unrelated keeps its place after Tweak.
    assert order == ["Overhaul.esp", "Tweak.esp", "Unrelated.esp"]


def test_existing_order_kept_when_unconstrained():
    meta = Metadata([])
    current = ["C.esp", "A.esp", "B.esp"]
    result = sort_plugins([P(n) for n in ("A.esp", "B.esp", "C.esp", "New.esp")], meta, current)
    assert names(result) == ["C.esp", "A.esp", "B.esp", "New.esp"] and result.moved == 0


def test_moved_plugin_goes_only_as_early_as_needed():
    meta = Metadata([{"plugins": [{"name": "C.esp", "after": []}, {"name": "A.esp", "after": ["C.esp"]}]}])
    plugins = [P(n, meta=meta.for_plugin(n)) for n in ("A.esp", "B.esp", "C.esp")]
    # C must load before A: C moves just in front of A, and A and B keep their order.
    assert names(sort_plugins(plugins, meta, ["A.esp", "B.esp", "C.esp"])) == ["C.esp", "A.esp", "B.esp"]


def test_masters_sorted_separately():
    meta = Metadata([])
    plugins = [P("Mod.esp"), P("Lib.esm", master=True)]
    assert names(sort_plugins(plugins, meta, ["Mod.esp", "Lib.esm"])) == ["Lib.esm", "Mod.esp"]


def test_cycle_is_reported():
    meta = Metadata([{"plugins": [{"name": "A.esp", "after": ["B.esp"]}, {"name": "B.esp", "after": ["A.esp"]}]}])
    with pytest.raises(CycleError) as err:
        sort_plugins([P(n, meta=meta.for_plugin(n)) for n in ("A.esp", "B.esp")], meta, [])
    assert "A.esp" in str(err.value) and "load after" in str(err.value)


def test_manager_sort_and_problems(env):
    import zlib

    root = add_mod(env, "Mods", {})
    for name in ("Late.esp", "Early.esp", "Mod.esp", "Needs.esp"):
        make_plugin(root / name, masters=["Skyrim.esm"])
    dirty = make_plugin(root / "Dirty.esp", masters=["Skyrim.esm"], records=[0x800])
    crc = zlib.crc32(dirty.read_bytes())
    # A fresh cached masterlist: no download needed.
    cache = masterlist.cache_dir("skyrimse")
    cache.mkdir(parents=True)
    (cache / "masterlist.yaml").write_text(MASTERLIST.replace("{crc}", f"{crc:08X}"))
    mgr = Manager(env)
    mgr.modlist.set_enabled(["Mods"], True)
    result, entries = mgr.loot_sort()
    order = [e.name for e in entries]
    assert order[:3] == ["Skyrim.esm", "Update.esm", "Dawnguard.esm"]
    assert order.index("Early.esp") < order.index("Mod.esp") < order.index("Late.esp")
    mgr.apply_plugin_order(entries)
    assert [e.name for e in mgr.plugins()] == order

    found = {p.key: p for p in mgr.problems()}
    assert "loot:dirty:dirty.esp" in found and "3 identical-to-master" in found["loot:dirty:dirty.esp"].title
    assert found["loot:dirty:dirty.esp"].url == "https://example.com/sseedit"
    warn = [p for k, p in found.items() if k.startswith("loot:msg:dirty.esp")]
    assert len(warn) == 1 and warn[0].title.endswith("Use Something Better instead.")
    assert "loot:req:needs.esp:wanted.esp" in found


def test_masterlist_update_uses_cache_when_fresh(env, monkeypatch):
    cache = masterlist.cache_dir("skyrimse")
    cache.mkdir(parents=True)
    (cache / "masterlist.yaml").write_text("plugins: []\n")
    assert masterlist.update("skyrimse") == cache / "masterlist.yaml"  # no network needed
    old = time.time() - 2 * masterlist.MAX_AGE
    os.utime(cache / "masterlist.yaml", (old, old))
    # Stale and offline (the test server isn't reachable): the cached copy is still used.
    monkeypatch.setattr(masterlist, "URL", "http://127.0.0.1:9/{repo}/{branch}")
    assert masterlist.update("skyrimse") == cache / "masterlist.yaml"
