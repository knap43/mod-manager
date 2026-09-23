import os
from pathlib import Path

from conftest import add_mod, make_plugin

from modmanager.core import proton
from modmanager.core.installer import Installer, detect_data_root, guess_info
from modmanager.core.instance import Executable
from modmanager.core.manager import Manager
from modmanager.core.modlist import ModList
from modmanager.core.plugins import read_header, update_saved_order


def test_modlist_uses_mo2_format(env):
    add_mod(env, "A", {"a.txt": "a"})
    add_mod(env, "B", {"b.txt": "b"})
    ml = ModList(env)
    ml.set_enabled(["A"], True)
    ml.move(["A"], len(ml.mods))  # A to highest priority
    text = env.active_profile.modlist_file.read_text().splitlines()
    assert text[1:] == ["+A", "-B"]  # highest priority first, like MO2
    assert [m.name for m in ModList(env).mods] == ["B", "A"]


def test_conflicts_and_plan(env):
    add_mod(env, "Low", {"Textures/x.dds": "low", "only_low.txt": "l"})
    add_mod(env, "High", {"textures/X.dds": "high"})
    ml = ModList(env)
    ml.move(["High"], len(ml.mods))
    ml.set_enabled(["Low", "High"], True)
    assert ml.conflicts["High"].overwrites == {"Low"}
    assert ml.conflicts["Low"].overwritten_by == {"High"}
    rel, src = ml.deployment_plan()["textures/x.dds"]
    assert src.read_text() == "high"


def test_deploy_and_purge_restore_game_folder(env):
    data = env.data_dir
    add_mod(env, "Tex", {"textures/vanilla.dds": "modded", "textures/new/n.dds": "n", "MESHES/m.nif": "m"})
    mgr = Manager(env)
    mgr.modlist.set_enabled(["Tex"], True)
    result = mgr.deploy()
    assert not result.errors
    # Case-insensitive merge into the existing "Textures" folder.
    assert (data / "Textures" / "vanilla.dds").read_text() == "modded"
    assert (data / "Textures" / "new" / "n.dds").read_text() == "n"
    assert not (data / "textures").exists() or (data / "textures").samefile(data / "Textures")
    src = env.mods_dir / "Tex" / "textures" / "new" / "n.dds"
    assert os.stat(data / "Textures" / "new" / "n.dds").st_ino == src.stat().st_ino
    assert not mgr.needs_deploy()

    mgr.purge()
    assert (data / "Textures" / "vanilla.dds").read_text() == "vanilla"
    assert not (data / "Textures" / "new").exists()
    assert not (data / "MESHES").exists()
    assert sorted(p.name for p in data.iterdir()) == ["Dawnguard.esm", "Skyrim.esm", "Textures", "Update.esm"]


def test_incremental_deploy_on_disable(env):
    add_mod(env, "A", {"a.txt": "a"})
    add_mod(env, "B", {"a.txt": "b"})
    mgr = Manager(env)
    mgr.modlist.set_enabled(["A", "B"], True)
    mgr.deploy()
    winner = mgr.modlist.mods[-1].name
    assert (env.data_dir / "a.txt").read_text() == winner.lower()
    mgr.modlist.set_enabled([winner], False)
    assert mgr.needs_deploy()
    mgr.deploy()
    assert (env.data_dir / "a.txt").read_text() != winner.lower()


def test_edits_in_game_folder_are_saved_to_mod(env):
    add_mod(env, "A", {"plugin.esp": "original"})
    mgr = Manager(env)
    mgr.modlist.set_enabled(["A"], True)
    mgr.deploy()
    target = env.data_dir / "plugin.esp"
    # A tool writes a new file and renames it over the deployed one.
    tmp = env.data_dir / "plugin.esp.tmp"
    tmp.write_text("edited")
    os.replace(tmp, target)
    mgr.deploy()
    assert (env.mods_dir / "A" / "plugin.esp").read_text() == "edited"
    assert os.stat(target).st_ino == (env.mods_dir / "A" / "plugin.esp").stat().st_ino


def test_capture_new_files_into_overwrite(env):
    mgr = Manager(env)
    mgr.deploy()
    before = mgr.snapshot_data()
    (env.data_dir / "SKSE").mkdir()
    (env.data_dir / "SKSE" / "generated.ini").write_text("x")
    moved = mgr.capture_new_files(before)
    assert moved == ["SKSE/generated.ini"]
    assert (env.overwrite_dir / "SKSE" / "generated.ini").read_text() == "x"
    # Redeployed from Overwrite so the game still sees it.
    assert (env.data_dir / "SKSE" / "generated.ini").read_text() == "x"


def test_plugins_and_game_files(env):
    root = add_mod(env, "P", {})
    make_plugin(root / "Mod.esp", masters=["Skyrim.esm", "Missing.esm"])
    make_plugin(root / "Light.esp", light=True)
    make_plugin(root / "Master.esm", master=True)
    mgr = Manager(env)
    mgr.modlist.set_enabled(["P"], True)
    entries = mgr.plugins()
    names = [e.name for e in entries]
    assert names[:3] == ["Skyrim.esm", "Update.esm", "Dawnguard.esm"]
    assert names.index("Master.esm") < names.index("Mod.esp")
    mod = next(e for e in entries if e.name == "Mod.esp")
    assert mod.missing_masters == ["Missing.esm"]
    light = next(e for e in entries if e.name == "Light.esp")
    assert light.load_index == "FE:000"

    mgr.deploy()
    plugins_txt = proton.appdata_local(Path(env.proton["prefix"])) / "Skyrim Special Edition" / "plugins.txt"
    lines = plugins_txt.read_text(encoding="cp1252").splitlines()
    assert "*Mod.esp" in lines and "*Master.esm" in lines
    assert "*Skyrim.esm" not in lines


def test_saved_order_keeps_absent_plugins():
    saved = [("A.esp", True), ("Gone.esp", False), ("B.esp", True)]
    assert update_saved_order(saved, [("B.esp", True), ("A.esp", False)]) == [
        ("B.esp", True), ("Gone.esp", False), ("A.esp", False),
    ]


def test_read_header(tmp_path):
    p = make_plugin(tmp_path / "x.esp", masters=["Skyrim.esm"], light=True)
    h = read_header(p)
    assert h.masters == ["Skyrim.esm"] and h.light_flag and not h.master_flag


def test_installer_detects_data_root(env, tmp_path):
    staging = tmp_path / "staging"
    (staging / "Wrapper" / "Data" / "meshes").mkdir(parents=True)
    (staging / "Wrapper" / "Data" / "meshes" / "a.nif").write_text("x")
    (staging / "Wrapper" / "readme.txt").write_text("hi")
    root, confident = detect_data_root(staging, env.game)
    assert confident and root == staging / "Wrapper" / "Data"
    name = Installer(env.mods_dir, env.tmp_dir, env.game).install(root, "My Mod", info=guess_info(Path("x-1-2-3-1700000000.7z")))
    assert (env.mods_dir / name / "meshes" / "a.nif").exists()


def test_guess_nexus_info():
    info = guess_info(Path("SkyUI_5_2_SE-12604-5-2SE-1573000000.7z"))
    assert (info.name, info.nexus_id, info.version) == ("SkyUI_5_2_SE", 12604, "5.2SE")


def test_build_launch(tmp_path):
    umu = tmp_path / "umu-run"
    umu.write_text("#!/bin/sh\n")
    umu.chmod(0o755)
    cfg = {"umu_run": str(umu), "prefix": str(tmp_path / "pfx"), "game_id": "umu-489830",
           "proton_path": "GE-Proton", "env": {"DXVK_HUD": "fps"}}
    spec = proton.build_launch(cfg, "/games/skyrim/SkyrimSE.exe", "-foo 'a b'", base_env={})
    assert spec.argv == [str(umu), "/games/skyrim/SkyrimSE.exe", "-foo", "a b"]
    assert spec.env["GAMEID"] == "umu-489830" and spec.env["PROTONPATH"] == "GE-Proton"
    assert spec.env["DXVK_HUD"] == "fps" and spec.cwd == "/games/skyrim"
    native = proton.build_launch(cfg, "/usr/bin/true", base_env={})
    assert native.argv == ["/usr/bin/true"]


def test_switch_method_and_copy_edits(env):
    add_mod(env, "A", {"textures/a.dds": "a"})
    mgr = Manager(env)
    mgr.modlist.set_enabled(["A"], True)
    mgr.deploy()
    mgr.change_deploy_method("copy")
    target = env.data_dir / "Textures" / "a.dds"
    assert target.read_text() == "a"
    assert target.stat().st_ino != (env.mods_dir / "A" / "textures" / "a.dds").stat().st_ino
    target.write_text("changed in game")
    mgr.purge()
    assert (env.mods_dir / "A" / "textures" / "a.dds").read_text() == "changed in game"
    assert not target.exists()
    mgr.change_deploy_method("symlink")
    mgr.deploy()
    assert target.is_symlink()
    spec = mgr.launch_spec(Executable("x", "/usr/bin/true"))
    assert str(env.path) in spec.env["PRESSURE_VESSEL_FILESYSTEMS_RW"]


def test_skyrim_le_uses_timestamps(tmp_path, monkeypatch):
    from modmanager.core.instance import Instance

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    game = tmp_path / "game"
    for name in ("Skyrim.esm", "Update.esm"):
        make_plugin(game / "Data" / name, master=True)
    inst = Instance.create(tmp_path / "inst", "LE", "skyrim", game)
    root = add_mod(inst, "M", {})
    make_plugin(root / "B.esp")
    make_plugin(root / "A.esp")
    mgr = Manager(inst)
    mgr.modlist.set_enabled(["M"], True)
    mgr.deploy()
    order = [e.name for e in mgr.plugins()]
    mtimes = [(inst.data_dir / n).stat().st_mtime for n in order]
    assert mtimes == sorted(mtimes)
    active = (mgr.plugin_file_dir() / "plugins.txt").read_text(encoding="cp1252").split()
    assert active[:2] == ["Skyrim.esm", "Update.esm"]
