from conftest import add_mod, make_plugin
from test_script_extender import fake_exe

from modmanager.core.manager import Manager
from modmanager.core.plugins import PluginEntry, fix_master_order


def keys(mgr):
    return {p.key: p for p in mgr.problems()}


def test_clean_setup_has_no_problems(env):
    assert Manager(env).problems() == []


def test_missing_master_resolved_by_enabling_mod(env):
    make_plugin(add_mod(env, "Framework", {}) / "Framework.esm", master=True)
    make_plugin(add_mod(env, "Addon", {}) / "Addon.esp", masters=["Skyrim.esm", "Framework.esm"])
    mgr = Manager(env)
    mgr.modlist.set_enabled(["Addon"], True)
    problem = keys(mgr)["master:framework.esm"]
    assert "disabled mod Framework" in problem.title and problem.fix_label == "Enable Framework"
    problem.fix()
    assert "master:framework.esm" not in keys(mgr)


def test_missing_master_resolved_by_enabling_plugin(env):
    root = add_mod(env, "Both", {})
    make_plugin(root / "Base.esm", master=True)
    make_plugin(root / "Patch.esp", masters=["Base.esm"])
    mgr = Manager(env)
    mgr.modlist.set_enabled(["Both"], True)
    mgr.set_plugins_enabled(["Base.esm"], False)
    problem = keys(mgr)["master:base.esm"]
    assert problem.fix_label == "Enable Base.esm"
    problem.fix()
    assert mgr.problems() == []


def test_master_not_installed(env):
    make_plugin(add_mod(env, "Orphan", {}) / "Orphan.esp", masters=["Gone.esm"])
    mgr = Manager(env)
    mgr.modlist.set_enabled(["Orphan"], True)
    problem = keys(mgr)["master:gone.esm"]
    assert problem.fix is None and "Orphan.esp" in problem.detail


def test_late_master_fix():
    a = PluginEntry("A.esp", masters=["B.esp"])
    b = PluginEntry("B.esp", is_master=False)
    c = PluginEntry("C.esp", masters=["A.esp"])
    order = fix_master_order([a, c, b])
    assert [e.name for e in order] == ["B.esp", "A.esp", "C.esp"]


def test_late_master_problem_and_fix(env):
    root = add_mod(env, "M", {})
    make_plugin(root / "Lib.esp")
    make_plugin(root / "User.esp", masters=["Lib.esp"])
    mgr = Manager(env)
    mgr.modlist.set_enabled(["M"], True)
    assert [e.name for e in mgr.plugins()][-2:] == ["Lib.esp", "User.esp"]  # new plugins arrive ordered
    env.active_profile.plugins_file.write_text("*Skyrim.esm\n*Update.esm\n*Dawnguard.esm\n*User.esp\n*Lib.esp\n")  # user reorders badly
    problem = keys(mgr)["order:masters"]
    problem.fix()
    names = [e.name for e in mgr.plugins()]
    assert names.index("Lib.esp") < names.index("User.esp")


def test_script_extender_and_address_library(env):
    fake_exe(env.game_dir / "SkyrimSE.exe", (1, 6, 1170, 0))
    add_mod(env, "Plugin", {"SKSE/Plugins/Thing.dll": "x"})
    mgr = Manager(env)
    mgr.modlist.set_enabled(["Plugin"], True)
    assert "se:missing" in keys(mgr)

    fake_exe(env.game_dir / "skse64_loader.exe", (0, 2, 2, 6))
    (env.game_dir / "skse64_1_6_640.dll").write_bytes(b"")
    mgr.script_extender_status(refresh=True)
    found = keys(mgr)
    assert "se:version" in found and "se:missing" not in found
    assert "32444" in found["se:addresslib"].url

    (env.game_dir / "skse64_1_6_640.dll").unlink()
    (env.game_dir / "skse64_1_6_1170.dll").write_bytes(b"")
    add_mod(env, "Address Library", {"SKSE/Plugins/versionlib-1-6-1170-0.bin": "x"})
    mgr.modlist.refresh()
    mgr.modlist.set_enabled(["Plugin", "Address Library"], True)
    mgr.script_extender_status(refresh=True)
    assert mgr.problems() == []


def test_nexus_requirements(env):
    add_mod(env, "Needs", {"a.txt": "a"})
    add_mod(env, "Framework", {"b.txt": "b"})
    mgr = Manager(env)
    needs, framework = mgr.modlist.get("Needs"), mgr.modlist.get("Framework")
    needs.meta.update(nexus_id=10, nexus_requirements=[
        {"mod_id": 20, "name": "Framework", "url": "https://x/20", "game": "skyrimspecialedition", "external": False},
        {"mod_id": 30, "name": "Elsewhere", "url": "https://x/30", "game": "skyrimspecialedition", "external": False},
        {"mod_id": None, "name": "Some website", "url": "", "game": "skyrimspecialedition", "external": True},
        {"mod_id": 30379, "name": "SKSE64", "url": "", "game": "skyrimspecialedition", "external": False},
    ])
    framework.meta.update(nexus_id=20)
    mgr.modlist.set_enabled(["Needs"], True)
    found = keys(mgr)
    assert set(found) == {"req:20", "req:30", "req:30379"}
    assert found["req:20"].fix_label == "Enable Framework"
    assert found["req:30"].url == "https://x/30" and found["req:30"].fix is None
    found["req:20"].fix()
    mgr.ignore_problem("req:30")
    fake_exe(env.game_dir / "skse64_loader.exe", (0, 2, 2, 6))
    mgr.script_extender_status(refresh=True)
    assert mgr.problems() == []
    assert {p.key for p in mgr.problems(include_ignored=True)} == {"req:30"}


def test_plugin_limit(env):
    root = add_mod(env, "Many", {})
    for i in range(260):
        make_plugin(root / f"P{i:03}.esp")
    mgr = Manager(env)
    mgr.modlist.set_enabled(["Many"], True)
    assert "limit:plugins" in keys(mgr)
