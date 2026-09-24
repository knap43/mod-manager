from conftest import add_mod

from modmanager.core.modlist import ModList


def build(env):
    """Order: Loose, [A] a1 a2, [B] b1, Last"""
    for name in ("Loose", "a1", "a2", "b1", "Last"):
        add_mod(env, name, {f"{name}.txt": name})
    ml = ModList(env)
    ml.create_separator("A", 0)
    ml.create_separator("B", 0)
    order = ["Loose", "A_separator", "a1", "a2", "B_separator", "b1", "Last"]
    ml.mods.sort(key=lambda m: order.index(m.name))
    ml.save()
    return ml


def names(ml):
    return [m.name for m in ml.mods]


def test_groups_and_collapsed_rows(env):
    ml = build(env)
    assert [m.name for m in ml.group_members("A_separator")] == ["a1", "a2"]
    assert [m.name for m in ml.group_members("B_separator")] == ["b1", "Last"]
    assert ml.collapsed_rows() == set()
    ml.set_collapsed(["A_separator"], True)
    assert ml.collapsed_rows() == {2, 3}
    # Persisted in the separator's meta.json, not the profile.
    assert ModList(env).is_collapsed(ModList(env).get("A_separator"))


def test_collapsed_separator_moves_with_its_mods(env):
    ml = build(env)
    ml.set_collapsed(["A_separator"], True)
    ml.move(ml.with_collapsed_members(["A_separator"]), len(ml.mods))
    assert names(ml) == ["Loose", "B_separator", "b1", "Last", "A_separator", "a1", "a2"]
    ml.set_collapsed(["A_separator"], False)
    assert ml.with_collapsed_members(["A_separator"]) == ["A_separator"]  # expanded: just the separator


def test_drop_below_collapsed_separator_lands_after_group(env):
    ml = build(env)
    ml.set_collapsed(["A_separator"], True)
    # The drop line right under "A" (row 2) or in its hidden rows means after a2 (row 4).
    assert ml.drop_index(2) == 4
    assert ml.drop_index(3) == 4
    assert ml.drop_index(4) == 4
    assert ml.drop_index(1) == 1           # above the separator: unchanged
    ml.set_collapsed(["A_separator"], False)
    assert ml.drop_index(2) == 2           # expanded: into the group as usual


def test_separator_of(env):
    ml = build(env)
    assert ml.separator_of(ml.index_of("Loose")) is None
    assert ml.separator_of(ml.index_of("a2")).name == "A_separator"
    assert ml.separator_of(ml.index_of("Last")).name == "B_separator"
